"""
Една задача от уеб интерфейса = един отделен процес: `python -m src.web.worker '<json>'`.

Защо отделен процес, а не нишка в сървъра:
  - „Спри" трябва да спира ВЕДНАГА. Конвейерът проверява should_cancel само между стъпките,
    а една стъпка (Критикът) мери до 8+ минути на живо (2026-10-13). Процес се убива веднага.
  - Няколко поста наведнъж. Логърът на конвейера (logging_utils) е общ за процеса — две
    пускания в един процес биха смесили run.log и хода на работата. Всеки процес има свой.

Протоколът към сървъра е по един JSON обект на ред в stdout:
  {"stage": "Пиша чернова…", "step": 2}   — нов етап (същите човешки етапи като в REPL)
  {"draft": "..."}                         — първата чернова (on_draft)
  {"ev": {"type": ..., "t": сек}}           — подробен ход за „Ход на работата" в интерфейса:
      step (нова стъпка), line (ред от run.log), call / call_end (едно извикване към модела —
      роля, модел, брой снимки, време, повторни опити), http (заявка към модела, № на опита),
      http_fail (неуспешна заявка — таймаут, HTTP код). Така се вижда КЪДЕ отива времето.
  {"state": "done"}                        — резултатът е на диска
  {"state": "error", "error": "...", "detail": "..."}
Всичко друго в stdout се игнорира от сървъра. Грешките и traceback-ите отиват в stderr
(сървърът ги пише в web-worker.log в папката на поста).

"done" идва СЛЕД фоновата финална проверка [7] и самопоправката [8] (research-plan-38) — „Готово"
значи проверено. main() пак изчаква нишката за всеки случай (repick/стари пътища), иначе
изходът от процеса би я убил и final_check би останал "pending" завинаги.
"""

from __future__ import annotations

import itertools
import json
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.config import ConfigError, load_config
from src.errors import AgentError
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline import runner
from src.repl import _HUMAN_STAGES, _STEP_ORDER, _STEP_TAG_RE

# Стъпките в лентата за напредък (без [7] — тя е фонова).
GENERATE_STEPS = [s for s in _STEP_ORDER if s != "[7]"]
FINAL_CHECK_WAIT_SECONDS = 600


class Timeline:
    """Подробните събития за „Ход на работата" — с време от началото на задачата."""

    def __init__(self, out: Callable[[dict[str, Any]], None]) -> None:
        self._out = out
        self._started = time.monotonic()
        self._ids = itertools.count(1)
        self.local = threading.local()  # кое извикване към модела тече в тази нишка
        self._last_tag: str | None = None

    def now(self) -> float:
        return round(time.monotonic() - self._started, 1)

    def send(self, event: dict[str, Any]) -> None:
        self._out({"ev": {**event, "t": self.now()}})

    def next_id(self) -> int:
        return next(self._ids)

    def log_line(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("==="):
            return
        match = _STEP_TAG_RE.match(message)
        tag = match.group(1) if match else None
        if tag and tag != self._last_tag:
            self._last_tag = tag
            self.send({"type": "step", "tag": tag})
        text = message[match.end():].strip() if match else message.strip()
        self.send({
            "type": "line",
            "tag": tag or self._last_tag,
            "level": "warn" if record.levelno >= logging.WARNING else "info",
            "text": text[:400],
        })


def _short_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "RunawayOutputError":
        return "моделът зацикли в празен текст — прекъснато, питам пак"
    if "Timeout" in name:
        return "изтече времето за отговор"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"HTTP {status}"
    if "Connection" in name:
        return "няма връзка със сървъра на моделите"
    return f"{name}: {exc}"[:160]


def instrument(client: Any, timeline: Timeline) -> None:
    """
    Наблюдава клиента отвън (без промяна в конвейера): всяко complete() е едно „извикване",
    всяко _post_chat() — една HTTP заявка в него (повторни опити, резервни модели, поправки на JSON).
    """
    original_complete = getattr(client, "complete", None)
    if original_complete is None:
        return  # тестов заместител без модел — няма какво да се наблюдава
    original_post = getattr(client, "_post_chat", None)
    config = getattr(client, "config", None)

    def complete(role: Any, prompt: str, *args: Any, **kwargs: Any) -> Any:
        call_id = timeline.next_id()
        timeline.local.call = call_id
        timeline.local.http = 0
        try:
            from src.llm.roles import model_for_role

            model = model_for_role(role, config) if config is not None else "?"
        except Exception:  # noqa: BLE001 — само за показване
            model = "?"
        images = kwargs.get("images") or []
        timeline.send({
            "type": "call", "id": call_id, "role": getattr(role, "value", str(role)), "model": model, "images": len(images),
        })
        started = time.monotonic()
        before = len(getattr(client, "call_log", []))
        try:
            result = original_complete(role, prompt, *args, **kwargs)
        except Exception as exc:
            timeline.send({
                "type": "call_end", "id": call_id, "ok": False, "dur": round(time.monotonic() - started, 1),
                "err": _short_error(exc),
            })
            raise
        log = getattr(client, "call_log", [])
        entry = log[-1] if len(log) > before else None
        timeline.send({
            "type": "call_end",
            "id": call_id,
            "ok": True,
            "dur": round(time.monotonic() - started, 1),
            "json_retries": max(0, entry.attempts - 1) if entry is not None and kwargs.get("schema") is not None else 0,
            "fallback": bool(entry.used_fallback) if entry is not None else False,
        })
        return result

    def post_chat(model: str, *args: Any, **kwargs: Any) -> Any:
        call_id = getattr(timeline.local, "call", None)
        timeline.local.http = getattr(timeline.local, "http", 0) + 1
        timeline.send({"type": "http", "id": call_id, "model": model, "n": timeline.local.http})
        started = time.monotonic()
        try:
            return original_post(model, *args, **kwargs)
        except Exception as exc:
            timeline.send({
                "type": "http_fail", "id": call_id, "model": model, "dur": round(time.monotonic() - started, 1),
                "err": _short_error(exc),
            })
            raise

    client.complete = complete
    if original_post is not None:
        client._post_chat = post_chat


class _StageHandler(logging.Handler):
    """Превежда логовете на конвейера („[4] Критик…") в човешки етап и го праща към сървъра."""

    def __init__(self, out: Callable[[dict[str, Any]], None], timeline: Timeline | None = None) -> None:
        super().__init__(level=logging.INFO)
        self._out = out
        self._timeline = timeline
        self._last: tuple[str, int] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if self._timeline is not None:
            self._timeline.log_line(record)
        match = _STEP_TAG_RE.match(record.getMessage())
        if not match:
            return
        tag = match.group(1)
        stage = _HUMAN_STAGES.get(tag)
        step = GENERATE_STEPS.index(tag) + 1 if tag in GENERATE_STEPS else 0
        if not stage and not step:
            return
        current = (stage or "", step)
        if current == self._last:
            return
        self._last = current
        message: dict[str, Any] = {}
        if stage:
            message["stage"] = stage
        if step:
            message["step"] = step
        self._out(message)


def await_final_check(post_dir: Path, out: Callable[[dict[str, Any]], None]) -> None:
    """
    „Готово" значи проверено (research-plan-38): задачата свършва СЛЕД финалната проверка [7] и
    самопоправката [8], не преди тях. Иначе постът влизаше в „Готови", а текстът и бележките се
    сменяха минути по-късно — и записаният в чата текст вече не беше този в поста.
    """
    name = f"final-check-{post_dir.name}"
    threads = [t for t in threading.enumerate() if t.name == name]
    if not threads:
        return
    out({"stage": "Последна проверка на готовия текст…"})
    for thread in threads:
        thread.join(timeout=FINAL_CHECK_WAIT_SECONDS)


def run(spec: dict[str, Any], out: Callable[[dict[str, Any]], None], client_factory: Callable[[], Any] | None = None) -> int:
    """Изпълнява задачата и докладва през `out`. Връща код за изход (0 = успех)."""
    timeline = Timeline(out)
    handler = _StageHandler(out, timeline)
    register_persistent_handler(handler)
    try:
        if client_factory is not None:
            client = client_factory()
        else:
            from src.llm.client import LLMClient

            client = LLMClient(load_config())
        instrument(client, timeline)
        post_dir = Path(spec["post_dir"])
        kind = spec["kind"]
        if kind == "generate":
            runner.run_pipeline(
                post_dir, client, force=True, mode=spec.get("mode"), on_draft=lambda text: out({"draft": text})
            )
        elif kind == "revise":
            runner.run_revision(post_dir, client, spec["instruction"], mode=spec.get("mode"))
        elif kind == "repick":
            runner.refresh_photos(post_dir, client)
        else:
            raise ValueError(f"непозната задача: {kind}")
        if kind in ("generate", "revise"):
            await_final_check(post_dir, out)
        out({"state": "done"})
        return 0
    except ConfigError as exc:
        out({"state": "error", "error": str(exc), "detail": None})
    except AgentError as exc:
        out({"state": "error", "error": exc.human_message, "detail": exc.detail})
    except Exception as exc:  # noqa: BLE001 — човекът трябва да види нещо, не мълчание
        traceback.print_exc()
        out({
            "state": "error",
            "error": "Неочаквана грешка. Подробностите са в web-worker.log и run.log в папката на поста.",
            "detail": f"{type(exc).__name__}: {exc}",
        })
    finally:
        unregister_persistent_handler(handler)
    return 1


_EMIT_LOCK = threading.Lock()  # Писател и избор на снимки текат паралелно — без смесени редове


def _emit(message: dict[str, Any]) -> None:
    # ensure_ascii: кирилицата минава като \uXXXX — без зависимост от кодировката на конзолата.
    line = json.dumps(message, ensure_ascii=True) + "\n"
    with _EMIT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    spec = json.loads(args[0])
    code = run(spec, _emit)
    for thread in threading.enumerate():
        if thread.name.startswith("final-check-"):
            thread.join(timeout=FINAL_CHECK_WAIT_SECONDS)
    return code


if __name__ == "__main__":
    sys.exit(main())
