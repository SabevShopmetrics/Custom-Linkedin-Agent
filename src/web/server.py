"""
Уеб интерфейс за агента — `python -m src.cli web` (или двоен клик на start_web.bat).

Същият конвейер като REPL/CLI (runner.run_pipeline / run_revision / refresh_photos), само
с браузър вместо терминал: бриф, качване на снимки и ZIP архиви, избор кои снимки вижда
агентът, ход на работата на живо и преглед „както ще изглежда в LinkedIn" под него.

Нарочно без нови зависимости — само стандартната библиотека (http.server). start.bat не
преинсталира пакети в вече направена .venv (маркерът .venv/.installed), затова нова
зависимост би счупила всяка съществуваща инсталация мълчаливо.

Файловата система остава единственото състояние (виж src/web/uploads.py за снимките):
  briefing.md, output/post.md — както при CLI
  output/edited.md             — ръчната редакция на човека в браузъра (post.md остава
                                 предложението на агента, за да се мери edit_ratio)
  output/published.md          — какво реално е публикувано (същото като /publikuvano)
  web.json                     — режим за дължина и дали името на папката е временно

Една задача към модела наведнъж за целия сървър: логърът на конвейера е общ за процеса
(logging_utils), а моделът е един и същ — две паралелни пускания биха смесили хода на
работата и биха се забавили взаимно.

Достъп отвън („онлайн"): по подразбиране сървърът слуша само на 127.0.0.1. За достъп от
други компютри — WEB_HOST=0.0.0.0 и задължително WEB_PASSWORD в .env (без парола сървърът
отказва да тръгне на външен адрес). Сложете го зад HTTPS (nginx/Caddy) — Basic Auth по
обикновен HTTP праща паролата некриптирана.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from src.config import ConfigError, child_environ, load_config
from src.humanize import humanize_notes, status_label
from src.pipeline import modes, runner
from src.pipeline.util import read_json, read_text_or_none, write_json
from src.quality.metrics import edit_ratio
from src.repl import slugify, unique_post_dir
from src.publish import linkedin as publisher
from src.web import batches, schedule, uploads
from src.web.worker import GENERATE_STEPS

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8765

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_SCHEDULE_BYTES = 10 * 1024 * 1024

DEFAULT_MAX_JOBS = 3  # колко поста пишат наведнъж (WEB_MAX_JOBS) — моделът е общ за всички
MAX_EVENTS = 5000  # таван за едно пускане — „Ход на работата" не бива да расте безкрайно
TRASH_DIR = "_kosh"  # „изтрити" постове — преместени, не изтрити безвъзвратно
_POST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
_CLIENT_HEADER = "X-Requested-With"
_CLIENT_HEADER_VALUE = "linkedin-agent"


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- задачи


@dataclass
class Job:
    """Едно пускане към модела (пълен пост, ревизия или нов избор на снимки)."""

    id: str
    post_id: str
    kind: str  # "generate" | "revise" | "repick"
    state: str = "running"  # running | done | error | cancelled
    stage: str = "Започвам…"
    step: int = 0
    total: int = len(GENERATE_STEPS)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    draft: str | None = None
    error: str | None = None
    detail: str | None = None
    cancel_requested: bool = False
    proc: Any = field(default=None, repr=False)  # работният процес (src/web/worker.py)
    events: list[dict[str, Any]] = field(default_factory=list, repr=False)  # „Ход на работата"

    def to_dict(self) -> dict[str, Any]:
        end = self.finished or time.time()
        return {
            "id": self.id,
            "post_id": self.post_id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "step": self.step,
            "total": self.total,
            "elapsed": round(end - self.started, 1),
            "has_draft": bool(self.draft),
            "error": self.error,
            "detail": self.detail,
            "cancel_requested": self.cancel_requested,
        }


# --------------------------------------------------------------------------- приложение


class WebApp:
    """Всичко освен HTTP: пътища, състояние на пост, задачи към модела."""

    def __init__(
        self,
        posts_dir: Path,
        *,
        password: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        in_process: bool = False,
        worker_command: Callable[[dict[str, Any]], list[str]] | None = None,
        max_jobs: int = DEFAULT_MAX_JOBS,
        publish_settings: publisher.PublishSettings | None = None,
        publish_session: Any = None,
    ) -> None:
        """
        `in_process` + `client_factory` — само за тестове: задачата тече в нишка с подменен
        конвейер, вместо в отделен процес с истинския модел. `worker_command` подменя командата
        за процеса (тестове на спирането).
        """
        self.posts_dir = posts_dir
        self.password = password or None
        self._client_factory = client_factory
        self._in_process = in_process
        self._worker_command = worker_command
        self.max_jobs = max(1, max_jobs)
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        # Опашка за пакети от график: постове, чакащи свободно място (до max_jobs наведнъж).
        self.queue: list[str] = []
        self.queue_errors: dict[str, str] = {}  # пост → защо не тръгна от опашката
        self._pump_lock = threading.RLock()  # _pump → generate → _finish → _pump
        self._publish_settings = publish_settings
        self._publish_session = publish_session

    # ---------------------------------------------------------------- конфигурация

    def config_problem(self) -> str | None:
        if self._client_factory is not None:
            return None
        try:
            load_config()
        except ConfigError as exc:
            return str(exc)
        return None

    # ---------------------------------------------------------------- постове

    def post_dir(self, post_id: str) -> Path:
        if not _POST_ID_RE.match(post_id) or ".." in post_id:
            raise HttpError(404, "Няма такъв пост.")
        path = self.posts_dir / post_id
        if not path.is_dir():
            raise HttpError(404, "Няма такъв пост.")
        return path

    def create_post(self, brief: str, mode: str | None) -> str:
        brief = (brief or "").strip()
        path = unique_post_dir(self.posts_dir, slugify(brief) if brief else "nov-post")
        (path / uploads.INCLUDED_DIR).mkdir(parents=True)
        if brief:
            (path / "briefing.md").write_text(brief + "\n", encoding="utf-8")
        write_json(
            path / "web.json",
            {"mode": modes.resolve_mode(mode) or modes.DEFAULT_MODE, "auto_name": True, "messages": []},
        )
        return path.name

    def update_post(self, post_id: str, brief: str | None, mode: str | None, title: str | None = None) -> None:
        path = self.post_dir(post_id)
        if title is not None:
            # Преименуване на сесията в историята — само етикет; папката и файловете не се пипат.
            web = _read_web(path)
            clean = " ".join(title.split())[:120]
            if clean:
                web["title"] = clean
            else:
                web.pop("title", None)  # празно име → пак името от брифа
            write_json(path / "web.json", web)
        if brief is not None:
            (path / "briefing.md").write_text(brief.strip() + "\n", encoding="utf-8")
        if mode is not None:
            web = _read_web(path)
            web["mode"] = modes.resolve_mode(mode) or modes.DEFAULT_MODE
            write_json(path / "web.json", web)

    def list_posts(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.posts_dir.is_dir():
            return items
        for path in self.posts_dir.iterdir():
            if not path.is_dir() or path.name.startswith(("_", ".")):
                continue
            post_md = path / "output" / "post.md"
            web_json = path / "web.json"
            stamp = max(
                path.stat().st_mtime,
                post_md.stat().st_mtime if post_md.exists() else 0,
                web_json.stat().st_mtime if web_json.exists() else 0,  # нов ред в чата
            )
            meta = _read_meta(path)
            items.append({
                "id": path.name,
                "title": _read_web(path).get("title")
                or _brief_title(read_text_or_none(path / "briefing.md") or "")
                or "(без бриф)",
                "status": meta.get("status") if post_md.exists() else None,
                "published": (path / "output" / "published.md").exists(),
                "modified": stamp,
                "batch": _read_web(path).get("batch"),
            })
        items.sort(key=lambda item: item["modified"], reverse=True)
        return items[:300]

    def post_state(self, post_id: str) -> dict[str, Any]:
        path = self.post_dir(post_id)
        web = _read_web(path)
        meta = _read_meta(path)
        output = path / "output"
        proposed = (read_text_or_none(output / "post.md") or "").strip()
        edited = (read_text_or_none(output / "edited.md") or "").strip()

        try:
            media = {item.get("filename"): item for item in read_json(path / "media.json").get("selected", [])}
        except (OSError, ValueError):
            media = {}
        in_post = uploads.selected_photos(path)
        photos = []
        for entry in uploads.list_photo_entries(path):
            agent = media.get(entry["name"]) or {}
            photos.append({
                **entry,
                "in_post": entry["name"] in in_post,
                "agent_reason": agent.get("reason"),
                "possible_minor": bool(agent.get("possible_minor")),
                "mismatch": agent.get("mismatch_reason") if agent.get("topical_mismatch") else None,
            })

        job = self.job_for(post_id)
        has_post = bool(proposed)
        brief = (read_text_or_none(path / "briefing.md") or "").strip()
        return {
            "id": post_id,
            "title": web.get("title") or _brief_title(brief),
            "brief": brief,
            "messages": self._messages(path, brief, proposed, meta),
            "pending": self.pending(path),
            "mode": web.get("mode") or modes.DEFAULT_MODE,
            "photos": photos,
            "in_post": in_post,
            "max_agent_photos": uploads.MAX_AGENT_PHOTOS,
            "post": {
                "text": edited or proposed,
                "proposed": proposed,
                "edited": bool(edited),
                "status": meta.get("status"),
                "status_label": status_label(meta.get("status") or ""),
                "notes": humanize_notes(meta.get("open_notes", []) + meta.get("warnings", [])),
                "fixes": meta.get("applied_fixes", []),
                "final_check": (meta.get("final_check") or {}).get("status"),
                "insufficient_brief": bool(meta.get("insufficient_brief")),
                "published": (output / "published.md").exists(),
                "seconds": meta.get("pipeline_seconds"),
            } if has_post else None,
            "draft": job.draft if job and job.state == "running" else None,
            "last_run": _read_json_or_none(path / "web-run.json"),
            "job": job.to_dict() if job else None,
        }

    def save_text(self, post_id: str, text: str) -> None:
        path = self.post_dir(post_id)
        proposed = (read_text_or_none(path / "output" / "post.md") or "").strip()
        if not proposed:
            raise HttpError(409, "Още няма готов пост за редактиране.")
        edited_path = path / "output" / "edited.md"
        if text.strip() == proposed:
            edited_path.unlink(missing_ok=True)  # същото като предложението — няма редакция
        else:
            edited_path.write_text(text.strip() + "\n", encoding="utf-8")

    def mark_published(self, post_id: str, text: str | None) -> float:
        path = self.post_dir(post_id)
        output = path / "output"
        proposed = (read_text_or_none(output / "post.md") or "").strip()
        if not proposed:
            raise HttpError(409, "Още няма готов пост.")
        published = (text or "").strip() or (read_text_or_none(output / "edited.md") or "").strip() or proposed
        (output / "published.md").write_text(published + "\n", encoding="utf-8")
        ratio = edit_ratio(proposed, published)
        changed = "без промени" if ratio == 0 else f"около {round(ratio * 100)}% променен"
        self._append_message(path, {"role": "agent", "kind": "note", "text": f"Отбелязан като публикуван ({changed})."})
        return ratio

    # ---------------------------------------------------------------- задачи

    def running_jobs(self) -> list[Job]:
        return [job for job in self.jobs.values() if job.state == "running"]

    def job_for(self, post_id: str) -> Job | None:
        """Последната задача на поста (текуща или приключила) — за изгледа."""
        for job in reversed(list(self.jobs.values())):
            if job.post_id == post_id:
                return job
        return None

    def _ensure_idle_for(self, post_id: str) -> None:
        if any(job.post_id == post_id for job in self.running_jobs()):
            raise HttpError(409, "Агентът работи по този пост в момента — изчакайте да приключи или натиснете „Спри“.")

    def _check_capacity(self, post_id: str) -> None:
        self._ensure_idle_for(post_id)
        running = len(self.running_jobs())
        if running >= self.max_jobs:
            raise HttpError(
                409,
                f"Агентът вече пише {running} поста наведнъж — изчакайте един да приключи "
                f"(или спрете някой от историята вляво).",
            )

    def start_job(self, post_id: str, kind: str, spec: dict[str, Any], *, total: int | None = None) -> Job:
        with self._lock:
            self._check_capacity(post_id)
            job = Job(id=uuid.uuid4().hex[:12], post_id=post_id, kind=kind)
            if total is not None:
                job.total = total
            self.jobs[job.id] = job
        spec = {**spec, "kind": kind, "post_dir": str((self.posts_dir / post_id).resolve())}
        if self._in_process:
            threading.Thread(target=self._run_in_thread, args=(job, spec), daemon=True, name=f"web-job-{job.id}").start()
        else:
            try:
                job.proc = self._spawn(job, spec)
            except OSError as exc:
                self._finish(job, "error", "Не успях да пусна агента на този компютър.", str(exc))
                return job
            threading.Thread(target=self._watch, args=(job,), daemon=True, name=f"web-watch-{job.id}").start()
        return job

    def _spawn(self, job: Job, spec: dict[str, Any]) -> subprocess.Popen:
        command = (self._worker_command or _default_worker_command)(spec)
        log_path = self.posts_dir / job.post_id / "web-worker.log"
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # без изскачащ черен прозорец
        with log_path.open("ab") as log:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                cwd=os.getcwd(),
                # Без стойностите от .env, заредени при пускане на сървъра — детето чете .env
                # наново, иначе промяна в .env не важи до рестарт (виж config.child_environ).
                env={**child_environ(), "PYTHONIOENCODING": "utf-8"},
                **kwargs,
            )

    def _watch(self, job: Job) -> None:
        """Чете хода на работата от процеса, ред по ред, до края му."""
        proc = job.proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(message, dict):
                self._on_message(job, message)
        code = proc.wait()
        self._on_exit(job, code)

    def _run_in_thread(self, job: Job, spec: dict[str, Any]) -> None:
        """Същата работа без отделен процес — само за тестове (подменен конвейер, без модел)."""
        from src.web import worker

        code = worker.run(spec, lambda message: self._on_message(job, message), client_factory=self._client_factory)
        self._on_exit(job, code)

    def _on_message(self, job: Job, message: dict[str, Any]) -> None:
        if job.state != "running":
            return
        if message.get("stage"):
            job.stage = str(message["stage"])
        if message.get("step"):
            job.step = max(job.step, int(message["step"]))
        if message.get("draft"):
            job.draft = str(message["draft"])
        if isinstance(message.get("ev"), dict) and len(job.events) < MAX_EVENTS:
            job.events.append(message["ev"])
        if message.get("state") in ("done", "error"):
            # Процесът може да продължи още малко (финалната проверка [7]) — резултатът е готов.
            self._finish(job, message["state"], message.get("error"), message.get("detail"))

    def _on_exit(self, job: Job, code: int) -> None:
        if job.state != "running":
            return
        if job.cancel_requested:
            self._finish(job, "cancelled", "Спряно по ваше желание. Каквото е написано дотук, остава в папката на поста.")
        else:
            self._finish(
                job,
                "error",
                "Агентът спря неочаквано. Подробностите са в web-worker.log в папката на поста.",
                f"код на изход {code}",
            )

    def _finish(self, job: Job, state: str, error: str | None = None, detail: str | None = None) -> None:
        with self._finish_lock:
            if job.state != "running":
                return
            job.error = error
            job.detail = detail
            if state == "done":
                job.step = job.total
                job.stage = "Готово"
            try:
                self._record_outcome(job, state)
                self._save_run(job, state)
            except Exception:  # noqa: BLE001 — историята на чата не бива да скрие резултата
                traceback.print_exc()
            # Състоянието се сменя ПОСЛЕДНО: страницата презарежда поста в момента, в който види
            # края — отговорът на агента в историята трябва вече да е записан.
            job.finished = time.time()
            job.state = state
        self._pump()  # освободи се място — следващият пост от опашката тръгва

    def cancel(self, job: Job) -> None:
        """„Спри" — веднага: процесът се убива, не чакаме края на текущата стъпка."""
        job.cancel_requested = True
        if job.state == "running":
            job.stage = "Спирам…"
        proc = job.proc
        if proc is not None and proc.poll() is None:
            proc.kill()
        if self._in_process and job.state == "running":
            self._on_exit(job, -1)

    def _stop_post_processes(self, post_id: str) -> None:
        """Спира всичко, което още пише в папката (вкл. финалната проверка след „Готово")."""
        for job in self.jobs.values():
            if job.post_id != post_id:
                continue
            if job.state == "running":
                self.cancel(job)
            proc = job.proc
            if proc is not None and proc.poll() is None:
                proc.kill()
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

    def delete_post(self, post_id: str) -> str:
        """
        „Изтрий" от историята: папката се мести в posts/_kosh/ — не се трие безвъзвратно (нищо
        от направеното не се губи; върнете я ръчно, ако е по грешка). Историята не показва
        папки, започващи с „_".
        """
        path = self.post_dir(post_id)
        self._stop_post_processes(post_id)
        trash = self.posts_dir / TRASH_DIR
        trash.mkdir(exist_ok=True)
        target = trash / post_id
        counter = 2
        while target.exists():
            target = trash / f"{post_id}-{counter}"
            counter += 1
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            raise HttpError(409, "Не успях да преместя поста в кошчето — файл е отворен. Опитайте след малко.") from exc
        return target.name

    def close(self) -> None:
        """Спира работещите процеси при спиране на сървъра."""
        self.queue.clear()
        for job in list(self.jobs.values()):
            proc = job.proc
            if proc is not None and proc.poll() is None:
                proc.kill()

    # ---------------------------------------------------------------- прикачени снимки
    #
    # Като в чат: снимките, качени след последното съобщение, са „прикачени" към следващото —
    # показват се над полето за писане. При изпращане отиват в самото съобщение (миниатюри в
    # балончето) и полето се изчиства. Агентът пак вижда всички включени снимки на поста.

    def pending(self, path: Path) -> list[str]:
        included = {e["name"] for e in uploads.list_photo_entries(path) if e["included"]}
        return [n for n in (_read_web(path).get("pending") or []) if n in included]

    def add_pending(self, path: Path, names: list[str]) -> None:
        with self._lock:
            web = _read_web(path)
            current = [n for n in (web.get("pending") or []) if n not in names]
            web["pending"] = current + list(names)
            write_json(path / "web.json", web)

    def drop_pending(self, path: Path, name: str) -> None:
        with self._lock:
            web = _read_web(path)
            if name in (web.get("pending") or []):
                web["pending"] = [n for n in web["pending"] if n != name]
                write_json(path / "web.json", web)

    def _take_pending(self, path: Path) -> list[str]:
        names = self.pending(path)
        with self._lock:
            web = _read_web(path)
            web["pending"] = []
            write_json(path / "web.json", web)
        return names

    def attach(self, post_id: str, from_post: str, names: list[str]) -> list[str]:
        """
        Снимките на стар промпт (↑ в полето за писане) се прикачват наново. От същия пост —
        включват се отново; от друг пост — копират се тук (оригиналът остава където е).
        """
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        source = self.post_dir(from_post)
        attached: list[str] = []
        for name in names:
            photo = uploads.find_photo(source, str(name))
            if photo is None:
                continue
            if source == path:
                uploads.set_included(path, photo.name, True)
                attached.append(photo.name)
            else:
                attached.append(uploads.copy_in(path, photo))
        self.add_pending(path, attached)
        return self.pending(path)

    def prompts(self, limit: int = 100) -> list[dict[str, Any]]:
        """Старите съобщения на човека от всички постове, най-новите първи — за ↑ / ↓."""
        items: list[dict[str, Any]] = []
        if not self.posts_dir.is_dir():
            return items
        for path in self.posts_dir.iterdir():
            if not path.is_dir() or path.name.startswith(("_", ".")):
                continue
            for message in _read_web(path).get("messages") or []:
                if message.get("role") != "user" or message.get("text") == "Избери снимките наново.":
                    continue
                photos = message.get("photos")
                items.append({
                    "text": message.get("text") or "",
                    "post_id": path.name,
                    "photos": photos if isinstance(photos, list) else [],
                    "at": message.get("at") or 0,
                })
        items.sort(key=lambda item: item["at"], reverse=True)
        unique: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for item in items:
            key = (item["text"], tuple(item["photos"]))
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique[:limit]

    # ---------------------------------------------------------------- история на чата

    def _append_message(self, path: Path, message: dict[str, Any]) -> None:
        """Историята на чата живее в web.json на поста — едно съобщение на ред, само добавяне."""
        with self._lock:
            web = _read_web(path)
            messages = list(web.get("messages") or [])
            messages.append({**message, "at": round(time.time())})
            web["messages"] = messages
            write_json(path / "web.json", web)

    def _save_run(self, job: Job, state: str) -> None:
        """Хронологията на последното пускане — за „къде отиде времето" и след края му."""
        path = self.posts_dir / job.post_id
        if not path.is_dir():
            return
        write_json(path / "web-run.json", {
            "kind": job.kind,
            "state": state,
            "seconds": round(time.time() - job.started, 1),
            "events": job.events,
        })

    def _record_outcome(self, job: Job, state: str) -> None:
        path = self.posts_dir / job.post_id
        if not path.is_dir():
            return
        if state == "cancelled":
            self._append_message(path, {"role": "agent", "kind": "stopped", "text": job.error or ""})
            return
        if state != "done":
            self._append_message(path, {"role": "agent", "kind": "error", "text": job.error or "", "detail": job.detail})
            return
        if job.kind == "repick":
            count = len(uploads.selected_photos(path))
            self._append_message(path, {"role": "agent", "kind": "note", "text": f"Избрах снимките наново — {count} в поста."})
            return
        meta = _read_meta(path)
        self._append_message(path, {
            "role": "agent",
            "kind": "post",
            "text": (read_text_or_none(path / "output" / "post.md") or "").strip(),
            "status": meta.get("status"),
            "insufficient_brief": bool(meta.get("insufficient_brief")),
        })

    def _messages(self, path: Path, brief: str, proposed: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
        web = _read_web(path)
        if "messages" in web:
            return web["messages"]
        # Пост, направен от терминала (REPL/CLI) — няма записан чат, сглобяваме го от файловете.
        synthesized: list[dict[str, Any]] = []
        if brief:
            synthesized.append({"role": "user", "text": brief})
        if proposed:
            synthesized.append({"role": "agent", "kind": "post", "text": proposed, "status": meta.get("status")})
        return synthesized

    def generate(self, post_id: str, brief: str, mode: str | None, message: str | None = None) -> Job:
        """`message` — какво е написал човекът в чата (при допълване на тънък бриф е само добавката)."""
        brief = (brief or "").strip()
        if not brief:
            raise HttpError(400, "Напишете какво се е случило — поне едно изречение.")
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        mode_key = modes.resolve_mode(mode) or _read_web(path).get("mode") or modes.DEFAULT_MODE

        uploads_count = uploads.included_count(path)
        if uploads_count > uploads.MAX_AGENT_PHOTOS * 2:
            raise HttpError(
                400,
                f"Включени са {uploads_count} снимки — изключете част от тях (до около "
                f"{uploads.MAX_AGENT_PHOTOS}), иначе агентът ще е много бавен.",
            )
        self._check_capacity(post_id)
        path, post_id = self._settle_name(path, brief)
        (path / "briefing.md").write_text(brief + "\n", encoding="utf-8")
        web = _read_web(path)
        web["mode"] = mode_key
        write_json(path / "web.json", web)
        self._append_message(path, {
            "role": "user", "text": (message or brief).strip(), "photos": self._take_pending(path), "mode": mode_key,
        })

        if (path / "output" / "post.md").exists():
            runner._archive_current_post(path)  # предишният вариант остава като post.vN.md
        _retire_edited(path)
        return self.start_job(post_id, "generate", {"mode": mode_key})

    def revise(self, post_id: str, instruction: str) -> Job:
        instruction = (instruction or "").strip()
        if not instruction:
            raise HttpError(400, "Напишете какво да се промени, напр. „по-кратко“.")
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        if not (path / "output" / "post.md").exists():
            raise HttpError(409, "Още няма готов пост — първо „Напиши поста“.")
        mode_key = modes.resolve_mode(_read_web(path).get("mode")) or modes.DEFAULT_MODE
        self._check_capacity(post_id)
        self._append_message(path, {"role": "user", "text": instruction, "photos": self._take_pending(path)})
        edited = _retire_edited(path)
        if edited:
            # Агентът редактира текста, който човекът вижда — с неговите поправки.
            runner._archive_current_post(path)
            (path / "output" / "post.md").write_text(edited + "\n", encoding="utf-8")
        job = self.start_job(post_id, "revise", {"mode": mode_key, "instruction": instruction}, total=1)
        job.stage = "Редактирам поста…"
        return job

    def repick(self, post_id: str) -> Job:
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        if not (path / "output" / "post.md").exists():
            raise HttpError(409, "Още няма готов пост — първо „Напиши поста“.")
        self._check_capacity(post_id)
        self._append_message(path, {"role": "user", "text": "Избери снимките наново."})
        job = self.start_job(post_id, "repick", {}, total=1)
        job.stage = "Избирам снимки…"
        return job

    def _settle_name(self, path: Path, brief: str) -> tuple[Path, str]:
        """
        Папка, създадена преди брифа да е ясен (напр. при първото качване на снимка), получава
        временно име. При първото пускане я преименуваме по брифа — както REPL именува постовете.
        """
        web = _read_web(path)
        if not web.get("auto_name") or (path / "run.log").exists():
            return path, path.name
        slug = slugify(brief)
        base = f"{date.today().isoformat()}-{slug}"
        if path.name == base or re.fullmatch(re.escape(base) + r"-\d+", path.name):
            target = path  # името вече отговаря на брифа — без излишно „-2"
        else:
            wanted = unique_post_dir(self.posts_dir, slug)
            try:
                path.rename(wanted)
                target = wanted
            except OSError:
                target = path  # Windows държи файл отворен — оставяме старото име, не е проблем
        web = _read_web(target)
        web["auto_name"] = False
        write_json(target / "web.json", web)
        return target, target.name

    # ---------------------------------------------------------------- пакети от график
    #
    # График → по един пост на ред, всички пуснати наведнъж. Сървърът пуска до max_jobs
    # паралелно; останалите чакат в опашката и тръгват сами, щом се освободи място.

    def create_batch(self, name: str, source: str | None, rows: list[schedule.ScheduleRow], mode: str | None) -> dict[str, Any]:
        if not rows:
            raise HttpError(400, "Графикът е празен — няма нито един ред с бриф.")
        default_mode = modes.resolve_mode(mode) or modes.DEFAULT_MODE
        batch = batches.make_batch(name, source, [])
        for row in rows:
            post_id = self.create_post(row.brief, row.mode or default_mode)
            path = self.posts_dir / post_id
            web = _read_web(path)
            web.update({"auto_name": False, "batch": batch["id"], "scheduled_for": row.date})
            if row.title:
                web["title"] = row.title
            write_json(path / "web.json", web)
            batch["items"].append({
                "post_id": post_id, "row": row.row, "date": row.date, "title": row.title, "photos": row.photos,
            })
        batches.save_batch(self.posts_dir, batch)
        return batch

    def batch(self, batch_id: str) -> dict[str, Any]:
        batch = batches.load_batch(self.posts_dir, batch_id)
        if batch is None:
            raise HttpError(404, "Няма такъв график.")
        return batch

    def list_batches(self) -> list[dict[str, Any]]:
        result = []
        for batch in batches.list_batches(self.posts_dir)[:50]:
            states = [self._item_state(item["post_id"]) for item in batch.get("items", [])]
            result.append({
                "id": batch["id"],
                "name": batch.get("name"),
                "created": batch.get("created"),
                "count": len(states),
                "done": sum(1 for s in states if s == "done"),
                "active": sum(1 for s in states if s in ("running", "queued")),
            })
        return result

    def start_batch(self, batch_id: str, post_ids: list[str] | None = None) -> int:
        """Слага постовете на пакета в опашката (без вече готовите, освен ако са поискани изрично)."""
        batch = self.batch(batch_id)
        members = [item["post_id"] for item in batch.get("items", [])]
        wanted = [p for p in (post_ids or members) if p in members]
        added = 0
        with self._pump_lock:
            running = {job.post_id for job in self.running_jobs()}
            for post_id in wanted:
                path = self.posts_dir / post_id
                if not path.is_dir() or post_id in running or post_id in self.queue:
                    continue
                if post_ids is None and (path / "output" / "post.md").exists():
                    continue  # „Пусни всички" не пише наново готовите — само изрично
                self.queue_errors.pop(post_id, None)
                self.queue.append(post_id)
                added += 1
        self._pump()
        return added

    def stop_batch(self, batch_id: str) -> None:
        members = {item["post_id"] for item in self.batch(batch_id).get("items", [])}
        with self._pump_lock:
            self.queue = [p for p in self.queue if p not in members]
        for job in self.running_jobs():
            if job.post_id in members:
                self.cancel(job)

    def _pump(self) -> None:
        """Пуска чакащите постове, докато има свободни места."""
        with self._pump_lock:
            while self.queue and len(self.running_jobs()) < self.max_jobs:
                post_id = self.queue.pop(0)
                path = self.posts_dir / post_id
                if not path.is_dir():
                    continue
                brief = (read_text_or_none(path / "briefing.md") or "").strip()
                try:
                    self.generate(post_id, brief, _read_web(path).get("mode"))
                except HttpError as exc:
                    self.queue_errors[post_id] = exc.message
                except Exception as exc:  # noqa: BLE001 — един лош ред не спира целия график
                    traceback.print_exc()
                    self.queue_errors[post_id] = f"Не успях да пусна поста ({type(exc).__name__})."

    def _item_state(self, post_id: str) -> str:
        job = self.job_for(post_id)
        if job is not None and job.state == "running":
            return "running"
        if post_id in self.queue:
            return "queued"
        path = self.posts_dir / post_id
        if (path / "output" / "post.md").exists():
            return "done"
        if job is not None and job.state in ("error", "cancelled"):
            return job.state
        if post_id in self.queue_errors:
            return "error"
        return "idle"

    def batch_status(self, batch_id: str, *, full: bool = False) -> dict[str, Any]:
        batch = self.batch(batch_id)
        items = [self._batch_item(index, item, full=full) for index, item in enumerate(batch.get("items", []), start=1)]
        counts: dict[str, int] = {}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        return {
            "id": batch["id"],
            "name": batch.get("name"),
            "source": batch.get("source"),
            "created": batch.get("created"),
            "items": items,
            "counts": counts,
            "max_jobs": self.max_jobs,
            "running_total": len(self.running_jobs()),
        }

    def _batch_item(self, index: int, item: dict[str, Any], *, full: bool) -> dict[str, Any]:
        post_id = item["post_id"]
        path = self.posts_dir / post_id
        exists = path.is_dir()
        web = _read_web(path) if exists else {}
        meta = _read_meta(path) if exists else {}
        brief = (read_text_or_none(path / "briefing.md") or "").strip() if exists else ""
        title = web.get("title") or item.get("title") or _brief_title(brief) or "(без бриф)"
        when = web.get("scheduled_for") or item.get("date")
        state = self._item_state(post_id) if exists else "missing"
        job = self.job_for(post_id)
        text = batches.final_text(path) if exists and state != "running" else ""
        in_post = uploads.selected_photos(path) if exists and text else []
        included = [e["name"] for e in uploads.list_photo_entries(path) if e["included"]] if exists else []
        running = job is not None and job.state == "running"
        result: dict[str, Any] = {
            "post_id": post_id,
            "index": index,
            "date": when,
            "weekday": batches.weekday(when),
            "title": title,
            "brief": brief[:400],
            "mode": web.get("mode") or modes.DEFAULT_MODE,
            "state": state,
            "queue_position": self.queue.index(post_id) + 1 if post_id in self.queue else None,
            "status": meta.get("status") if text else None,
            "status_label": status_label(meta.get("status") or "") if text else None,
            "notes": len(meta.get("open_notes", []) + meta.get("warnings", [])) if text else 0,
            "insufficient_brief": bool(meta.get("insufficient_brief")) if text else False,
            "words": len(text.split()) if text else 0,
            "snippet": text[:320],
            "edited": exists and (path / "output" / "edited.md").exists(),
            "published": exists and (path / "output" / "published.md").exists(),
            "photos_in_post": in_post,
            "photos_included": included[:8],
            "photo_count": len(in_post) if text else len(included),
            "file_name": batches.file_stem(index, {"date": when}, title) + ".md",
            "error": self.queue_errors.get(post_id) or (job.error if job and job.state in ("error", "cancelled") else None),
            "job": job.to_dict() if running else None,
            "draft": (job.draft or "")[:320] if running and job.draft else None,
            "seconds": meta.get("pipeline_seconds") if text else None,
        }
        if full:
            result["text"] = text
        return result

    # ---------------------------------------------------------------- публикуване

    def publish_settings(self) -> publisher.PublishSettings:
        return self._publish_settings or publisher.settings_from_env()

    def publish(self, post_id: str, text: str | None) -> dict[str, Any]:
        """
        Изрично действие на човек (бутон + потвърждение в браузъра). В режим draft нищо не
        се изпраща — записва се заявката; в режим linkedin постът излиза на страницата.
        """
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        current = batches.final_text(path)
        if not current:
            raise HttpError(409, "Още няма готов пост.")
        body = (text or "").strip() or current
        photos = [path / "output" / "selected" / name for name in uploads.selected_photos(path)]
        settings = self.publish_settings()
        try:
            result = publisher.publish(path / "output", body, photos, settings, session=self._publish_session)
        except publisher.PublishError as exc:
            raise HttpError(502 if settings.mode == "linkedin" else 400, exc.human_message) from exc
        if result.mode == "linkedin":
            self.mark_published(post_id, body)
            self._append_message(path, {"role": "agent", "kind": "note", "text": f"Публикуван в LinkedIn: {result.url}"})
        else:
            self._append_message(path, {
                "role": "agent", "kind": "note",
                "text": "Режим „чернова“: нищо не е изпратено към LinkedIn. Заявката е записана в "
                        "output/linkedin-request.json — копирайте текста и го публикувайте ръчно.",
            })
        return result.to_dict()


def _read_web(path: Path) -> dict[str, Any]:
    try:
        return read_json(path / "web.json")
    except (OSError, ValueError):
        return {}


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        return read_json(path / "output" / "meta.json")
    except (OSError, ValueError):
        return {}


def _retire_edited(path: Path) -> str | None:
    """Ръчната редакция се пази като edited.vN.md, преди нов текст от агента да я замени."""
    edited = path / "output" / "edited.md"
    if not edited.exists():
        return None
    text = edited.read_text(encoding="utf-8").strip()
    number = 1
    while (path / "output" / f"edited.v{number}.md").exists():
        number += 1
    edited.rename(path / "output" / f"edited.v{number}.md")
    return text


def _brief_title(brief: str) -> str:
    for line in brief.splitlines():
        line = line.strip().lstrip("#*>- ").strip()
        if line:
            return line[:110]
    return ""


def _mode_options() -> list[dict[str, Any]]:
    hints = {"kadar": "кратко, 40–90 думи", "fokus": "стандартно", "panorama": "дълго, 400–650 думи"}
    return [{"key": m.key, "label": m.label, "hint": hints.get(m.key, "")} for m in modes.MODES.values()]


# --------------------------------------------------------------------------- HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "LinkedInAgent/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WebApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — подпис от базовия клас
        pass  # прегледът пита за хода на всяка секунда — без шум в прозореца на сървъра

    # ---------------------------------------------------------------- изпращане

    def _send(self, status: int, body: bytes, content_type: str, *, cache: str = "no-store", extra: dict | None = None) -> None:
        if not getattr(self, "_body_consumed", True):
            self.close_connection = True  # неизчетено тяло би объркало следващата заявка
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path: Path, content_type: str, cache: str, extra: dict | None = None) -> None:
        self._send(200, path.read_bytes(), content_type, cache=cache, extra=extra)

    # ---------------------------------------------------------------- вход

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON_BYTES:
            raise HttpError(413, "Заявката е твърде голяма.")
        raw = self.rfile.read(length) if length else b""
        self._body_consumed = True
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(400, "Невалидна заявка.") from exc
        if not isinstance(data, dict):
            raise HttpError(400, "Невалидна заявка.")
        return data

    def _authorized(self) -> bool:
        password = self.app.password
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        _, _, given = decoded.partition(":")
        return hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))

    def _dispatch(self, method: str) -> None:
        self._body_consumed = not int(self.headers.get("Content-Length") or 0)
        if not self._authorized():
            self._send(
                401,
                "Нужна е парола.".encode("utf-8"),
                "text/plain; charset=utf-8",
                extra={"WWW-Authenticate": 'Basic realm="LinkedIn Agent", charset="UTF-8"'},
            )
            return
        # Заявки, които променят нещо, идват само от нашата страница: браузърът не позволява
        # на чужд сайт да сложи този заглавен ред без CORS разрешение (защита от CSRF —
        # иначе запомнената Basic Auth парола би се пратила и от чужда страница).
        if method != "GET" and self.headers.get(_CLIENT_HEADER) != _CLIENT_HEADER_VALUE:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_JSON_BYTES:
                self.rfile.read(length)  # иначе Windows понякога прекъсва връзката (RST) преди отговора
                self._body_consumed = True
            self._json({"error": "Заявката не идва от интерфейса на агента."}, 403)
            return

        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        for route_method, pattern, handler in _ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parsed.path)
            if not match:
                continue
            try:
                handler(self, *(unquote(group) for group in match.groups()), query=query)
            except HttpError as exc:
                self._json({"error": exc.message}, exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                self._json({"error": "Вътрешна грешка на сървъра — вижте прозореца на сървъра."}, 500)
            return
        self._json({"error": "Няма такъв адрес."}, 404)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ---------------------------------------------------------------- маршрути

    def r_index(self, *, query) -> None:
        self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8", "no-cache", extra={
            "Content-Security-Policy": (
                "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
            ),
        })

    def r_static(self, name: str, *, query) -> None:
        types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
        for candidate in STATIC_DIR.iterdir():
            if candidate.name == name and candidate.suffix in types:
                self._file(candidate, types[candidate.suffix], "no-cache")
                return
        raise HttpError(404, "Няма такъв файл.")

    def r_info(self, *, query) -> None:
        settings = self.app.publish_settings()
        self._json({
            "modes": _mode_options(),
            "default_mode": modes.DEFAULT_MODE,
            "config_error": self.app.config_problem(),
            "max_agent_photos": uploads.MAX_AGENT_PHOTOS,
            "max_jobs": self.app.max_jobs,
            "jobs": [job.to_dict() for job in self.app.running_jobs()],
            "queued": len(self.app.queue),
            "publish_mode": settings.mode,
            "publish_problem": settings.problem(),
        })

    def r_posts(self, *, query) -> None:
        self._json({"posts": self.app.list_posts()})

    def r_create(self, *, query) -> None:
        data = self._read_json()
        post_id = self.app.create_post(str(data.get("brief") or ""), data.get("mode"))
        self._json(self.app.post_state(post_id), 201)

    def r_post(self, post_id: str, *, query) -> None:
        self._json(self.app.post_state(post_id))

    def r_update(self, post_id: str, *, query) -> None:
        data = self._read_json()
        brief = data.get("brief")
        title = data.get("title")
        self.app.update_post(
            post_id, str(brief) if brief is not None else None, data.get("mode"), str(title) if title is not None else None
        )
        self._json({"ok": True})

    def r_upload(self, post_id: str, *, query) -> None:
        path = self.app.post_dir(post_id)
        self.app._ensure_idle_for(post_id)
        name = (query.get("name") or ["file"])[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise HttpError(400, "Празен файл.")
        if length > MAX_UPLOAD_BYTES:
            raise HttpError(413, "Файлът е над 2 GB — разделете архива на части.")

        incoming = path / ".incoming"
        incoming.mkdir(exist_ok=True)
        temp = incoming / f"{uuid.uuid4().hex}{Path(uploads.safe_filename(name)).suffix}"
        remaining = length
        with temp.open("wb") as out:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        self._body_consumed = True
        if remaining > 0:
            temp.unlink(missing_ok=True)
            raise HttpError(400, "Качването прекъсна — опитайте пак.")
        report = uploads.add_upload(path, name, temp)
        self.app.add_pending(path, report.added)
        self._json(report.to_dict())

    def r_photo_get(self, post_id: str, name: str, *, query) -> None:
        path = self.app.post_dir(post_id)
        size = (query.get("size") or ["s"])[0]
        try:
            thumb = uploads.thumbnail(path, name, size)
        except Exception:  # noqa: BLE001 — повредена снимка: просто без миниатюра
            thumb = None
        if thumb is None:
            raise HttpError(404, "Няма такава снимка.")
        self._file(thumb, "image/jpeg", "private, max-age=86400")

    def r_photo_set(self, post_id: str, name: str, *, query) -> None:
        path = self.app.post_dir(post_id)
        data = self._read_json()
        self.app._ensure_idle_for(post_id)
        included = bool(data.get("included"))
        if not uploads.set_included(path, name, included):
            raise HttpError(404, "Няма такава снимка.")
        if not included:
            self.app.drop_pending(path, name)
        self._json({"ok": True})

    def r_attach(self, post_id: str, *, query) -> None:
        data = self._read_json()
        names = [str(n) for n in data.get("names") or []]
        self._json({"pending": self.app.attach(post_id, str(data.get("from_post") or post_id), names)})

    def r_prompts(self, *, query) -> None:
        self._json({"prompts": self.app.prompts()})

    def r_selection(self, post_id: str, *, query) -> None:
        path = self.app.post_dir(post_id)
        data = self._read_json()
        self.app._ensure_idle_for(post_id)
        if not (path / "output" / "post.md").exists():
            raise HttpError(409, "Още няма готов пост.")
        names = [str(n) for n in data.get("names") or []]
        self._json({"in_post": uploads.set_selection(path, names)})

    def r_text(self, post_id: str, *, query) -> None:
        data = self._read_json()
        self.app.save_text(post_id, str(data.get("text") or ""))
        self._json({"ok": True})

    def r_published(self, post_id: str, *, query) -> None:
        data = self._read_json()
        ratio = self.app.mark_published(post_id, data.get("text"))
        self._json({"edit_ratio": ratio})

    def r_generate(self, post_id: str, *, query) -> None:
        data = self._read_json()
        message = data.get("message")
        job = self.app.generate(post_id, str(data.get("brief") or ""), data.get("mode"), str(message) if message else None)
        self._json({"job": job.to_dict(), "post_id": job.post_id}, 202)

    def r_revise(self, post_id: str, *, query) -> None:
        data = self._read_json()
        job = self.app.revise(post_id, str(data.get("instruction") or ""))
        self._json({"job": job.to_dict(), "post_id": job.post_id}, 202)

    def r_repick(self, post_id: str, *, query) -> None:
        self._read_json()
        job = self.app.repick(post_id)
        self._json({"job": job.to_dict(), "post_id": job.post_id}, 202)

    def r_job(self, job_id: str, *, query) -> None:
        job = self.app.jobs.get(job_id)
        if job is None:
            raise HttpError(404, "Няма такава задача.")
        since = (query.get("since") or ["0"])[0]
        since = int(since) if since.isdigit() else 0
        self._json({
            "job": job.to_dict(),
            "draft": job.draft if job.state == "running" else None,
            "events": job.events[since:],
            "events_from": since,
        })

    def r_cancel(self, job_id: str, *, query) -> None:
        self._read_json()
        job = self.app.jobs.get(job_id)
        if job is None:
            raise HttpError(404, "Няма такава задача.")
        self.app.cancel(job)
        self._json({"job": job.to_dict()})

    def r_delete(self, post_id: str, *, query) -> None:
        self._json({"trashed_as": self.app.delete_post(post_id)})

    def r_publish(self, post_id: str, *, query) -> None:
        data = self._read_json()
        text = data.get("text")
        self._json(self.app.publish(post_id, str(text) if text else None))

    # ---------------------------------------------------------------- график → пакет

    def _read_raw(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise HttpError(400, "Празен файл.")
        if length > limit:
            raise HttpError(413, "Файлът е твърде голям за график.")
        data = self.rfile.read(length)
        self._body_consumed = True
        return data

    def r_schedule_parse(self, *, query) -> None:
        name = (query.get("name") or ["grafik.txt"])[0]
        data = self._read_raw(MAX_SCHEDULE_BYTES)
        try:
            parsed = schedule.parse_schedule(name, data)
        except schedule.ScheduleError as exc:
            raise HttpError(400, str(exc)) from exc
        self._json(parsed.to_dict())

    def r_batches(self, *, query) -> None:
        self._json({"batches": self.app.list_batches()})

    def r_batch_create(self, *, query) -> None:
        data = self._read_json()
        rows = schedule.rows_from_payload(data.get("rows") if isinstance(data.get("rows"), list) else [])
        batch = self.app.create_batch(str(data.get("name") or ""), data.get("source"), rows, data.get("mode"))
        if data.get("start"):
            self.app.start_batch(batch["id"])
        self._json(self.app.batch_status(batch["id"]), 201)

    def r_batch(self, batch_id: str, *, query) -> None:
        full = (query.get("full") or ["0"])[0] == "1"
        self._json(self.app.batch_status(batch_id, full=full))

    def r_batch_start(self, batch_id: str, *, query) -> None:
        data = self._read_json()
        post_ids = data.get("post_ids")
        ids = [str(p) for p in post_ids] if isinstance(post_ids, list) else None
        added = self.app.start_batch(batch_id, ids)
        self._json({"added": added, **self.app.batch_status(batch_id)})

    def r_batch_stop(self, batch_id: str, *, query) -> None:
        self._read_json()
        self.app.stop_batch(batch_id)
        self._json(self.app.batch_status(batch_id))

    def r_batch_export(self, batch_id: str, *, query) -> None:
        status = self.app.batch_status(batch_id, full=True)
        body = batches.export_zip(self.app.posts_dir, status["items"])
        name = f"linkedin-{batch_id}.zip"
        self._send(200, body, "application/zip", extra={"Content-Disposition": f'attachment; filename="{name}"'})

    def r_batch_csv(self, batch_id: str, *, query) -> None:
        status = self.app.batch_status(batch_id, full=True)
        self._send(200, batches.result_csv(status["items"]), "text/csv; charset=utf-8")


_P = r"([^/]+)"
_ROUTES: list[tuple[str, re.Pattern[str], Callable[..., None]]] = [
    ("GET", re.compile(r"/"), _Handler.r_index),
    ("GET", re.compile(r"/static/([A-Za-z0-9._-]+)"), _Handler.r_static),
    ("GET", re.compile(r"/api/info"), _Handler.r_info),
    ("GET", re.compile(r"/api/posts"), _Handler.r_posts),
    ("POST", re.compile(r"/api/posts"), _Handler.r_create),
    ("GET", re.compile(rf"/api/posts/{_P}"), _Handler.r_post),
    ("PUT", re.compile(rf"/api/posts/{_P}"), _Handler.r_update),
    ("DELETE", re.compile(rf"/api/posts/{_P}"), _Handler.r_delete),
    ("POST", re.compile(rf"/api/posts/{_P}/upload"), _Handler.r_upload),
    ("GET", re.compile(rf"/api/posts/{_P}/photos/{_P}"), _Handler.r_photo_get),
    ("PUT", re.compile(rf"/api/posts/{_P}/photos/{_P}"), _Handler.r_photo_set),
    ("PUT", re.compile(rf"/api/posts/{_P}/selection"), _Handler.r_selection),
    ("POST", re.compile(rf"/api/posts/{_P}/attach"), _Handler.r_attach),
    ("GET", re.compile(r"/api/prompts"), _Handler.r_prompts),
    ("PUT", re.compile(rf"/api/posts/{_P}/text"), _Handler.r_text),
    ("POST", re.compile(rf"/api/posts/{_P}/published"), _Handler.r_published),
    ("POST", re.compile(rf"/api/posts/{_P}/generate"), _Handler.r_generate),
    ("POST", re.compile(rf"/api/posts/{_P}/revise"), _Handler.r_revise),
    ("POST", re.compile(rf"/api/posts/{_P}/repick"), _Handler.r_repick),
    ("GET", re.compile(rf"/api/jobs/{_P}"), _Handler.r_job),
    ("POST", re.compile(rf"/api/jobs/{_P}/cancel"), _Handler.r_cancel),
    ("POST", re.compile(rf"/api/posts/{_P}/publish"), _Handler.r_publish),
    ("POST", re.compile(r"/api/schedule/parse"), _Handler.r_schedule_parse),
    ("GET", re.compile(r"/api/batches"), _Handler.r_batches),
    ("POST", re.compile(r"/api/batches"), _Handler.r_batch_create),
    ("GET", re.compile(rf"/api/batches/{_P}"), _Handler.r_batch),
    ("POST", re.compile(rf"/api/batches/{_P}/start"), _Handler.r_batch_start),
    ("POST", re.compile(rf"/api/batches/{_P}/stop"), _Handler.r_batch_stop),
    ("GET", re.compile(rf"/api/batches/{_P}/export\.zip"), _Handler.r_batch_export),
    ("GET", re.compile(rf"/api/batches/{_P}/result\.csv"), _Handler.r_batch_csv),
]


# --------------------------------------------------------------------------- старт


def _default_worker_command(spec: dict[str, Any]) -> list[str]:
    return [sys.executable, "-m", "src.web.worker", json.dumps(spec, ensure_ascii=True)]


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def serve(
    *,
    host: str,
    port: int,
    password: str | None,
    posts_dir: Path,
    max_jobs: int = DEFAULT_MAX_JOBS,
    open_browser: Callable[[str], Any] | None = None,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> int:
    """
    Пуска сървъра до Ctrl+C. `open_browser` се подава само от start_web.bat (--open).

    `tls_cert`/`tls_key` (WEB_TLS_CERT/WEB_TLS_KEY) — HTTPS направо от сървъра, без nginx.
    Нужно е за достъп отвън: браузърът дава запис в папка на потребителя (File System Access
    API) само на https:// или localhost, а Basic Auth по http праща паролата некриптирана.
    """
    if not _is_loopback(host) and not password:
        print(
            "Отказвам да пусна интерфейса за достъп отвън без парола.\n"
            "Сложете WEB_PASSWORD=... в .env (или оставете WEB_HOST празно за достъп само от този компютър)."
        )
        return 2

    app = WebApp(posts_dir, password=password, max_jobs=max_jobs)
    try:
        server = make_server(app, host, port)
    except OSError as exc:
        app.close()
        print(f"Не мога да заема порт {port} ({exc}). Вероятно интерфейсът вече работи — отворете http://localhost:{port}")
        return 1

    scheme = "http"
    if tls_cert:
        import ssl

        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(tls_cert, tls_key or None)
        except (OSError, ssl.SSLError) as exc:
            server.server_close()
            app.close()
            print(f"Не мога да заредя сертификата за HTTPS ({exc}). Проверете WEB_TLS_CERT / WEB_TLS_KEY в .env.")
            return 1
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"

    local_url = f"{scheme}://localhost:{port}"
    print("LinkedIn агент — уеб интерфейс")
    print(f"  Отворете: {local_url}")
    if not _is_loopback(host):
        print(f"  Отвън:    {scheme}://<адреса-на-сървъра>:{port}  (с паролата от WEB_PASSWORD)")
        if scheme == "http":
            print("  ⚠ Без HTTPS браузърите на колегите няма да позволят запис в тяхна папка "
                  "(само четене + ZIP). Сложете WEB_TLS_CERT/WEB_TLS_KEY или сървъра зад nginx/Caddy.")
    problem = app.config_problem()
    if problem:
        print(f"  ⚠ {problem}")
    print("  Спиране: Ctrl+C или затворете този прозорец.")
    sys.stdout.flush()

    if open_browser is not None:
        threading.Timer(0.6, lambda: open_browser(local_url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
    return 0
