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
import hashlib
import hmac
import ipaddress
import json
import os
import socket
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
from datetime import date, datetime, timedelta
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
from src.web import alerts, assistant, batches, flow, insights, library, schedule, uploads
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
SCHEDULER_SECONDS = 30  # колко често се проверява дали насрочен пост е на ред
LATE_LIMIT_HOURS = 6  # закъснял повече (сървърът е бил спрян) → само напомняне, не публикуване
CONFIG_ALERT_HOURS = 24  # одобрен пост излиза до толкова часа, а настройките за LinkedIn липсват → сигнал
TOKEN_ALERT_DAYS = 7  # LINKEDIN_TOKEN_EXPIRES е след толкова дни → сигнал
PUBLISH_ALERT_KINDS = ("publish_failed", "publish_missed", "publish_config", "due_reminder")
REVIEW_ALERT_KINDS = ("writing_failed", "needs_review")
INGEST_SECONDS = 60  # колко често се гледа папката за нови пост-папки
ACTIVE_DAYS = 3  # „Преглед" показва насрочените, текущите и пипнатите в последните толкова дни
_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


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


# Версия на API-то, което страниците очакват (static/store.js → UI_VERSION). Различна → страницата
# казва „нужен е рестарт" (страниците се четат наново от диска, сървърът — само при рестарт).
# Вдига се при всяка промяна на адрес или полета, на които разчита интерфейсът.
UI_VERSION = "2026-10-14.6"

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
        alert_sender: alerts.Sender | None = None,
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
        self._stop = threading.Event()
        # Изпращач на сигнали навън (Teams/Slack/имейл). None — само в интерфейса. serve() го
        # взима от .env; тестовете подават подменен — никога мрежа от тестове.
        self.alert_sender = alert_sender

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
            web = _read_web(path)
            items.append({
                "id": path.name,
                "title": web.get("title")
                or _brief_title(read_text_or_none(path / "briefing.md") or "")
                or "(без бриф)",
                "status": meta.get("status") if post_md.exists() else None,
                "published": (path / "output" / "published.md").exists(),
                "modified": stamp,
                "batch": web.get("batch"),
                "mode": web.get("mode") or modes.DEFAULT_MODE,
                **self._schedule_info(path, web),
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
            "schedule": self._schedule_info(path, web),
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
        alerts.resolve_for_post(self.posts_dir, post_id, PUBLISH_ALERT_KINDS)
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
        try:
            self._review_alerts(job, state)
        except Exception:  # noqa: BLE001 — сигналът е удобство, не бива да спре опашката
            traceback.print_exc()
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
        self._stop.set()
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
        web.pop("needs_brief", None)  # празно място от план → вече има бриф
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
            "kind": batch.get("kind"),
            "created": batch.get("created"),
            "items": items,
            "counts": counts,
            "max_jobs": self.max_jobs,
            "running_total": len(self.running_jobs()),
            "summary": self._batch_summary(items),
        }

    def _batch_summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Обобщението на таблото (web-ui-5): време, колко остава, какво иска човек."""
        done = [i for i in items if i["state"] == "done"]
        seconds = [i["seconds"] for i in done if i.get("seconds")]
        avg = round(sum(seconds) / len(seconds)) if seconds else None
        left = sum(1 for i in items if i["state"] in ("running", "queued"))
        eta = None
        if avg and left:
            # Агентите пишат по max_jobs наведнъж: остават ceil(left / max_jobs) „вълни" по средното време.
            eta = round(-(-left // max(1, self.max_jobs)) * avg)
        dates = sorted(i["date"] for i in items if i.get("date"))
        return {
            "avg_seconds": avg,
            "eta_seconds": eta,
            "attention": sum(1 for i in items if i.get("attention")),
            "approved": sum(1 for i in items if i.get("approved")),
            "published": sum(1 for i in items if i.get("published")),
            "autofixed": sum(i.get("autofixed", 0) for i in items),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
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
        # web-ui-5: часът, одобрението и най-важната бележка — таблото е подробният изглед на пакета.
        sched = self._schedule_info(path, web) if exists else {}
        result.update({
            "time": sched.get("scheduled_time"),
            "approved": bool(sched.get("approved")),
            "digest": flow.note_digest(meta) if text and meta.get("status") != "ready" else "",
            "autofixed": sum(1 for f in meta.get("applied_fixes", []) or [] if str(f).startswith("Махнато след проверката")) if text else 0,
            "attention": bool(text) and (bool(meta.get("insufficient_brief")) or bool(meta.get("warnings"))
                                         or bool(sched.get("time_warning")) or bool(sched.get("approval_stale")))
                         or state in ("error", "cancelled"),
        })
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
            self.alert("publish_failed", "error", exc.human_message, post_id=post_id)
            raise HttpError(502 if settings.mode == "linkedin" else 400, exc.human_message) from exc
        except Exception as exc:  # мрежа/неочаквано — човекът трябва да разбере, че постът НЕ е излязъл
            self.alert("publish_failed", "error", f"Неочаквана грешка при връзката с LinkedIn ({type(exc).__name__}).",
                       post_id=post_id)
            raise HttpError(502, "Връзката с LinkedIn прекъсна — постът не е публикуван. Опитайте пак.") from exc
        if result.mode == "linkedin":
            alerts.resolve_for_post(self.posts_dir, post_id, PUBLISH_ALERT_KINDS)
            self.mark_published(post_id, body)
            self._append_message(path, {"role": "agent", "kind": "note", "text": f"Публикуван в LinkedIn: {result.url}"})
        else:
            self._append_message(path, {
                "role": "agent", "kind": "note",
                "text": "Режим „чернова“: нищо не е изпратено към LinkedIn. Заявката е записана в "
                        "output/linkedin-request.json — копирайте текста и го публикувайте ръчно.",
            })
        return result.to_dict()

    # ---------------------------------------------------------------- насрочване (web-ui-3)
    #
    # Постът има дата и час (web.json: scheduled_for, scheduled_time). „Одобрен" значи, че
    # човекът е видял ТОЧНО този текст и е съгласен да излезе в този час — пази се отпечатък
    # на текста; всяка промяна по текста отменя одобрението сама. Когато часът дойде:
    #   PUBLISH_MODE=linkedin → публикува се (само одобрен, само ако не е закъснял > 6 ч.);
    #   PUBLISH_MODE=draft    → бележка „време е" в чата на поста — човекът публикува ръчно.

    def _schedule_info(self, path: Path, web: dict[str, Any]) -> dict[str, Any]:
        approved = web.get("approved") if isinstance(web.get("approved"), dict) else None
        valid = False
        text = batches.final_text(path) if (approved or web.get("scheduled_for")) else ""
        if approved:
            valid = bool(text) and approved.get("hash") == _text_hash(text)
        time_warning = None
        if text and web.get("scheduled_for"):
            written = _written_on(path)
            time_warning = insights.relative_time_warning(text, web.get("scheduled_for"), written)
        return {
            "scheduled_for": web.get("scheduled_for"),
            "scheduled_time": web.get("scheduled_time"),
            "approved": valid,
            "approval_stale": bool(approved) and not valid,
            "needs_brief": bool(web.get("needs_brief")),
            "idea": web.get("idea") if web.get("needs_brief") else None,
            "album": web.get("album"),
            "due_notified": bool(web.get("due_notified")),
            "time_warning": time_warning,
        }

    def set_schedule(self, post_id: str, *, when: Any = None, at: Any = None, approved: Any = None) -> dict[str, Any]:
        path = self.post_dir(post_id)
        web = _read_web(path)
        if when is not None:
            if str(when).strip() == "":
                for key in ("scheduled_for", "scheduled_time", "approved", "due_notified"):
                    web.pop(key, None)
            else:
                iso = schedule.parse_date(when)
                if not iso:
                    raise HttpError(400, "Не разбирам датата — напр. 2026-11-20 или 20.11.2026.")
                web["scheduled_for"] = iso
                web.pop("due_notified", None)
        if at is not None:
            match = _TIME_RE.match(str(at).strip())
            if str(at).strip() and not match:
                raise HttpError(400, "Часът трябва да е като 10:00.")
            web["scheduled_time"] = f"{int(match.group(1)):02d}:{match.group(2)}" if match else None
            web.pop("due_notified", None)
        if approved is not None:
            if approved:
                text = batches.final_text(path)
                if not text:
                    raise HttpError(409, "Още няма текст за одобрение — първо агентът трябва да напише поста.")
                if not web.get("scheduled_for"):
                    raise HttpError(409, "Изберете дата, преди да одобрите поста за публикуване.")
                web["approved"] = {"hash": _text_hash(text), "at": round(time.time())}
                alerts.resolve_for_post(self.posts_dir, post_id, REVIEW_ALERT_KINDS)
                self._append_message(path, {
                    "role": "agent", "kind": "note",
                    "text": f"Одобрен за {web['scheduled_for']} {web.get('scheduled_time') or assistant.DEFAULT_TIME}.",
                })
            else:
                web.pop("approved", None)
            web.pop("due_notified", None)
        write_json(path / "web.json", web)
        return self._schedule_info(path, web)

    def scheduler_tick(self, now: datetime | None = None) -> list[str]:
        """Една проверка: насрочените постове, чийто час е дошъл. Връща кои са обработени."""
        now = now or datetime.now()
        handled: list[str] = []
        if not self.posts_dir.is_dir():
            return handled
        running = {job.post_id for job in self.running_jobs()}
        for path in self.posts_dir.iterdir():
            if not path.is_dir() or path.name.startswith(("_", ".")) or path.name in running:
                continue
            web = _read_web(path)
            if not web.get("scheduled_for") or web.get("due_notified") or (path / "output" / "published.md").exists():
                continue
            info = self._schedule_info(path, web)
            if not info["approved"]:
                continue
            try:
                due = datetime.fromisoformat(f"{web['scheduled_for']}T{web.get('scheduled_time') or assistant.DEFAULT_TIME}")
            except ValueError:
                continue
            if due > now:
                continue
            late = (now - due).total_seconds() > LATE_LIMIT_HOURS * 3600
            web["due_notified"] = round(time.time())
            write_json(path / "web.json", web)
            stamp = f"{web['scheduled_for']} {web.get('scheduled_time') or assistant.DEFAULT_TIME}"
            if self.publish_settings().mode == "linkedin" and not late:
                try:
                    self.publish(path.name, None)  # при грешка publish() сам вдига сигнал
                except HttpError as exc:
                    self._append_message(path, {"role": "agent", "kind": "note",
                                                "text": f"⏰ Насроченото публикуване ({stamp}) не мина: {exc.message}"})
            elif late:
                self._append_message(path, {"role": "agent", "kind": "note",
                                            "text": f"⏰ Часът ({stamp}) мина, докато агентът беше спрян — публикувайте ръчно."})
                self.alert("publish_missed", "error",
                           f"Трябваше да излезе на {stamp}, но агентът беше спрян. Постът НЕ е публикуван — "
                           "публикувайте го ръчно или изберете нов час.", post_id=path.name)
            else:
                self._append_message(path, {"role": "agent", "kind": "note", "text": (
                    f"⏰ Време е за публикуване ({stamp}). Режим „чернова“ — копирайте текста, публикувайте "
                    "го в LinkedIn и натиснете „Публикувах го“.")})
                self.alert("due_reminder", "action",
                           f"Насрочен за {stamp}. Режим „чернова“ — копирайте текста и го публикувайте ръчно.",
                           post_id=path.name)
            handled.append(path.name)
        self._check_publish_readiness(now)
        return handled

    def _check_publish_readiness(self, now: datetime) -> None:
        """
        Сигнали ПРЕДИ проблема: одобрен пост излиза до 24 ч., а LinkedIn настройките липсват;
        токенът изтича до 7 дни (LINKEDIN_TOKEN_EXPIRES, 60 дни без refresh — web-ui-2).
        """
        settings = self.publish_settings()
        if settings.mode != "linkedin":
            return
        problem = settings.problem()
        for path in self.posts_dir.iterdir():
            if not path.is_dir() or path.name.startswith(("_", ".")):
                continue
            web = _read_web(path)
            key = f"publish_config:{path.name}"
            if not web.get("scheduled_for") or (path / "output" / "published.md").exists():
                continue
            if not self._schedule_info(path, web)["approved"]:
                continue
            try:
                due = datetime.fromisoformat(f"{web['scheduled_for']}T{web.get('scheduled_time') or assistant.DEFAULT_TIME}")
            except ValueError:
                continue
            soon = timedelta(0) <= due - now <= timedelta(hours=CONFIG_ALERT_HOURS)
            if problem and soon:
                self.alert("publish_config", "warn",
                           f"Излиза на {web['scheduled_for']} {web.get('scheduled_time') or ''}, но: {problem}",
                           post_id=path.name)
            elif not problem:
                alerts.resolve_by_key(self.posts_dir, key)
        expires = os.environ.get("LINKEDIN_TOKEN_EXPIRES", "").strip()
        if expires:
            try:
                left = (date.fromisoformat(expires) - now.date()).days
            except ValueError:
                left = None
            if left is not None and left <= TOKEN_ALERT_DAYS:
                text = (f"Токенът за LinkedIn изтече на {expires}." if left < 0
                        else f"Токенът за LinkedIn изтича на {expires} (след {left} дни).")
                self.alert("token_expiring", "warn",
                           f"{text} Без нов токен насрочените постове няма да излязат — подновява се от Super Admin "
                           "на страницата (LINKEDIN_ACCESS_TOKEN и LINKEDIN_TOKEN_EXPIRES в .env).",
                           key=f"token_expiring:{expires}")

    # ---------------------------------------------------------------- папка → постове сами (web-ui-4)
    #
    # „Пуска папка и забравя": всяка НОВА подпапка със снимки и текст става пост — насрочен по
    # „Кога:" или името на папката — и тръгва да се пише. Само папки, появили се СЛЕД включването
    # (`ingest_since`): иначе избирането на стара папка би изписало наведнъж всичко в нея.

    # ---------------------------------------------------------------- настройки и туториал (web-ui-5)
    #
    # Туториалът се показва сам само веднъж: минат или пропуснат → помни се в posts/_settings.json
    # (на сървъра, не в браузъра — колежката може да отвори агента от друг браузър и пак да не я
    # спамим). Пуска се пак от „Настройки".

    def tutorial_state(self) -> dict[str, Any]:
        raw = library.settings(self.posts_dir).get("tutorial")
        if isinstance(raw, dict) and raw.get("state") in ("finished", "skipped"):
            return {"done": True, "state": raw["state"], "at": raw.get("at"), "step": raw.get("step")}
        return {"done": False, "state": None, "at": None, "step": None}

    def set_tutorial(self, state: str, step: Any = None) -> dict[str, Any]:
        if state == "reset":
            library.save_settings(self.posts_dir, {"tutorial": None})
        elif state in ("finished", "skipped"):
            value: dict[str, Any] = {"state": state, "at": round(time.time())}
            if isinstance(step, int) and step >= 0:
                value["step"] = step
            library.save_settings(self.posts_dir, {"tutorial": value})
        else:
            raise HttpError(400, "Непознато състояние на туториала.")
        return self.tutorial_state()

    def settings_view(self) -> dict[str, Any]:
        """Всичко универсално на едно място — за страницата „Настройки"."""
        publish = self.publish_settings()
        config = None
        try:
            config = load_config()
        except ConfigError:
            pass
        folder = self.library_folder()
        return {
            "tutorial": self.tutorial_state(),
            "folder": str(folder) if folder else None,
            "auto_ingest": self.ingest_settings()["auto"],
            "publish_mode": publish.mode,
            "publish_problem": publish.problem(),
            "max_jobs": self.max_jobs,
            "default_mode": modes.DEFAULT_MODE,
            "alert_channels": alerts.channels_from_env() if self.alert_sender else {"webhook": False, "email": False},
            "speed": {
                "no_thinking": sorted(config.no_thinking_roles) if config else [],
                "final_autofix": bool(config.final_autofix) if config else None,
                "structured_output": config.structured_output if config else None,
            },
        }

    def ingest_settings(self) -> dict[str, Any]:
        values = library.settings(self.posts_dir)
        return {"auto": bool(values.get("auto_ingest")), "since": values.get("ingest_since"),
                "ingested": values.get("ingested") or {}}

    def set_auto_ingest(self, on: bool) -> dict[str, Any]:
        values: dict[str, Any] = {"auto_ingest": bool(on)}
        if on and not library.settings(self.posts_dir).get("auto_ingest"):
            values["ingest_since"] = round(time.time())
        library.save_settings(self.posts_dir, values)
        return self.ingest_settings()

    def _post_folders(self) -> list[dict[str, Any]]:
        folder = self.library_folder()
        if folder is None:
            return []
        return [a for a in library.scan(folder)["albums"] if a.get("brief") and a["path"]]

    def ingest_tick(self) -> list[str]:
        cfg = self.ingest_settings()
        if not cfg["auto"]:
            return []
        created = []
        for album in self._post_folders():
            record = cfg["ingested"].get(album["path"])
            if record:
                self._mark_source_change(record, album)
                continue
            if album["path"] in {p.get("album") for p in self.list_posts() if p.get("album")}:
                continue  # вече е пост (през асистента) — не го дублираме
            if album["modified"] < (cfg["since"] or 0):
                continue  # стара папка — в „Входящи" за ръчно „Вземи"
            created.append(self.ingest_album(album["path"]))
        return created

    def ingest_album(self, rel: str) -> str:
        """Пост-папка → пост: бриф от текста, снимки, дата/час/режим от заглавните редове или името."""
        album = next((a for a in self._post_folders() if a["path"] == rel), None)
        if album is None:
            raise HttpError(404, f"В папката няма пост-папка „{rel}“ (снимки + текстов файл).")
        known = self.ingest_settings()["ingested"].get(rel)
        if known and (self.posts_dir / known.get("post_id", "")).is_dir():
            return known["post_id"]
        meta, body = flow.parse_header(album["brief"])
        if not body:
            raise HttpError(400, f"Текстът в „{rel}“ е празен.")
        when, at = flow.when_from(album["name"], meta)
        mode = flow.mode_from(meta) or modes.DEFAULT_MODE
        post_id = self.create_post(body, mode)
        path = self.posts_dir / post_id
        web = _read_web(path)
        web.update({"auto_name": False, "title": flow.title_from(album["name"], meta), "source": "folder",
                    "source_hash": album["brief_hash"]})
        if when and when >= date.today().isoformat():
            web.update({"scheduled_for": when, "scheduled_time": at or assistant.DEFAULT_TIME})
        write_json(path / "web.json", web)
        folder = self.library_folder()
        if folder is not None:
            self._copy_album(path, folder, rel)
        values = library.settings(self.posts_dir)
        ingested = values.get("ingested") or {}
        ingested[rel] = {"post_id": post_id, "hash": album["brief_hash"], "t": round(time.time())}
        library.save_settings(self.posts_dir, {"ingested": ingested})
        with self._pump_lock:
            if post_id not in self.queue:
                self.queue.append(post_id)
        self._pump()
        return post_id

    # ---------------------------------------------------------------- качена папка (web-ui-5)
    #
    # „Качи папка" / плъзната папка от компютъра: всяка подпапка (снимки + текст) става пост — без
    # да се избира „Папка" на сървъра. Браузърът праща текста; сървърът го разчита със същите
    # правила като пост-папките (flow.py): „Кога:", името на папката, „Режим:", „Заглавие:".
    # Снимките идват после през обичайното качване; писането тръгва с inbox_start.

    def inbox_batch(self, name: str) -> dict[str, Any]:
        """Едно качване на папка = един пакет със свое табло (#/b/<id>) — web-ui-5."""
        batch = batches.make_batch(name or "Качена папка", "папка от компютъра", [])
        batch["kind"] = "upload"
        batches.save_batch(self.posts_dir, batch)
        return {"id": batch["id"], "name": batch["name"]}

    def inbox_post(self, name: str, text: str, batch_id: str | None = None) -> dict[str, Any]:
        name = " ".join(str(name or "").split())[:200] or "Нов пост"
        meta, body = flow.parse_header(text or "")
        when, at = flow.when_from(name, meta)
        mode = flow.mode_from(meta) or modes.DEFAULT_MODE
        title = flow.title_from(name, meta)
        post_id = self.create_post(body, mode)
        path = self.posts_dir / post_id
        web = _read_web(path)
        web.update({"auto_name": False, "title": title, "source": "upload"})
        scheduled = bool(when and when >= date.today().isoformat())
        if scheduled:
            web.update({"scheduled_for": when, "scheduled_time": at or assistant.DEFAULT_TIME})
        if not body:
            web.update({"needs_brief": True, "idea": title})
        batch = batches.load_batch(self.posts_dir, batch_id) if batch_id else None
        if batch is not None:
            web["batch"] = batch["id"]
            with self._pump_lock:  # редовете идват един по един, но пакетът е общ файл
                batch = batches.load_batch(self.posts_dir, batch["id"]) or batch
                batch["items"].append({"post_id": post_id, "row": len(batch["items"]) + 1, "date": when if scheduled else None,
                                       "title": title, "photos": None})
                batches.save_batch(self.posts_dir, batch)
        write_json(path / "web.json", web)
        return {"id": post_id, "title": title, "when": when if scheduled else None,
                "at": (at or assistant.DEFAULT_TIME) if scheduled else None,
                "past": bool(when) and not scheduled, "mode": mode, "has_brief": bool(body)}

    def inbox_start(self, post_id: str) -> dict[str, Any]:
        path = self.post_dir(post_id)
        if not (read_text_or_none(path / "briefing.md") or "").strip():
            return {"queued": False}  # без текст — чака бриф във „Входящи"
        with self._pump_lock:
            if post_id not in self.queue and not self.job_for(post_id):
                self.queue.append(post_id)
        self._pump()
        return {"queued": True}

    def _mark_source_change(self, record: dict[str, Any], album: dict[str, Any]) -> None:
        path = self.posts_dir / record.get("post_id", "")
        if not path.is_dir() or record.get("hash") == album.get("brief_hash"):
            return
        web = _read_web(path)
        if web.get("source_hash") != album.get("brief_hash") and not web.get("source_changed"):
            web["source_changed"] = True
            write_json(path / "web.json", web)

    def rewrite_from_source(self, post_id: str) -> Job:
        """„Пиши наново" след промяна на текста в папката — новият текст става брифът."""
        path = self.post_dir(post_id)
        rel = _read_web(path).get("album")
        album = next((a for a in self._post_folders() if a["path"] == rel), None)
        if album is None:
            raise HttpError(409, "Не намирам папката на поста — пишете в чата му.")
        meta, body = flow.parse_header(album["brief"])
        web = _read_web(path)
        web.update({"source_hash": album["brief_hash"], "source_changed": False})
        write_json(path / "web.json", web)
        values = library.settings(self.posts_dir)
        ingested = values.get("ingested") or {}
        if rel in ingested:
            ingested[rel]["hash"] = album["brief_hash"]
            library.save_settings(self.posts_dir, {"ingested": ingested})
        return self.generate(post_id, body, flow.mode_from(meta), message="Текстът в папката е променен — пиша наново.")

    # ---------------------------------------------------------------- „Преглед" — целият път с един поглед

    def _post_view(self, item: dict[str, Any], running: dict[str, Job], queue: list[str]) -> dict[str, Any]:
        path = self.posts_dir / item["id"]
        web = _read_web(path)
        meta = _read_meta(path)
        has_text = bool(batches.final_text(path))
        try:
            media = read_json(path / "media.json").get("selected", [])
        except (OSError, ValueError):
            media = []
        selected = set(uploads.selected_photos(path)) if has_text else set()
        minors = sum(1 for m in media if m.get("possible_minor") and m.get("filename") in selected)
        job = self.job_for(item["id"])
        job_error = job.error if job and job.state == "error" else None
        overdue = False
        if has_text and item.get("approved") and item.get("scheduled_for") and not item["published"]:
            try:
                due = datetime.fromisoformat(f"{item['scheduled_for']}T{item.get('scheduled_time') or assistant.DEFAULT_TIME}")
                overdue = due <= datetime.now()
            except ValueError:
                pass
        problems = flow.issues(
            has_text=has_text, meta=meta, sched=item, minors=minors, job_error=job_error,
            source_changed=bool(web.get("source_changed")), published=item["published"],
            overdue=overdue, publish_mode=self.publish_settings().mode,
        )
        run = running.get(item["id"])
        cover = (uploads.selected_photos(path) or [e["name"] for e in uploads.list_photo_entries(path) if e["included"]] or [None])[0]
        return {
            **{k: item.get(k) for k in ("id", "title", "scheduled_for", "scheduled_time", "approved", "needs_brief",
                                       "published", "status", "album", "idea", "mode")},
            "lane": flow.lane(running=bool(run), queued=item["id"] in queue, has_text=has_text,
                              published=item["published"], problems=problems, needs_brief=item.get("needs_brief")),
            "issues": problems,
            "cover": cover,
            "source": web.get("source"),
            "words": len(batches.final_text(path).split()) if has_text else 0,
            "job": run.to_dict() if run else None,
            "queue_position": queue.index(item["id"]) + 1 if item["id"] in queue else None,
        }

    def overview(self) -> dict[str, Any]:
        today = date.today()
        since = time.time() - ACTIVE_DAYS * 86400
        running = {job.post_id: job for job in self.running_jobs()}
        queue = list(self.queue)
        cards = []
        for item in self.list_posts():
            active = (
                item["id"] in running or item["id"] in queue
                or (item.get("scheduled_for") and item["scheduled_for"] >= (today.isoformat()))
                or item["modified"] >= since
            )
            if not active:
                continue
            card = self._post_view(item, running, queue)
            if card["lane"] == "inbox" and not (item.get("needs_brief") or item.get("scheduled_for")):
                continue  # стара чернова без текст и без дата — тя е в историята, не в потока
            cards.append(card)
        lanes: dict[str, list[dict[str, Any]]] = {k: [] for k in ("inbox", "writing", "review", "ready", "published")}
        for card in cards:
            lanes[card["lane"]].append(card)
        key = lambda c: (c.get("scheduled_for") or "9999", c.get("scheduled_time") or "")  # noqa: E731
        for name in lanes:
            lanes[name].sort(key=key, reverse=name == "published")

        # Пост-папки, които още не са станали пост (стари или при изключено автоматично взимане)
        cfg = self.ingest_settings()
        linked = {p.get("album") for p in self.list_posts() if p.get("album")}  # взети и през асистента/„Пост от албума"
        waiting = []
        for album in self._post_folders():
            if album["path"] in cfg["ingested"] or album["path"] in linked:
                continue
            meta, _ = flow.parse_header(album["brief"])
            when, at = flow.when_from(album["name"], meta)
            waiting.append({"path": album["path"], "title": flow.title_from(album["name"], meta), "when": when,
                            "at": at, "count": album["count"], "cover": album["cover"]})

        finished = sorted((j for j in self.jobs.values() if j.state == "done" and j.kind == "generate" and j.finished),
                          key=lambda j: j.finished, reverse=True)[:10]
        upcoming = [c for c in lanes["ready"] + lanes["review"] + lanes["writing"] if c.get("scheduled_for")]
        upcoming.sort(key=key)
        return {
            "lanes": lanes,
            "folder_waiting": waiting,
            "attention": [c for c in lanes["review"] + lanes["inbox"] if c["issues"]],
            "next": upcoming[0] if upcoming else None,
            "agents": {
                "max_jobs": self.max_jobs,
                "running": [{"post_id": j.post_id, "title": next((c["title"] for c in cards if c["id"] == j.post_id), j.post_id),
                             **j.to_dict()} for j in running.values()],
                "queued": [{"post_id": p, "title": next((c["title"] for c in cards if c["id"] == p), p), "position": i + 1}
                           for i, p in enumerate(queue)],
                "avg_seconds": round(sum(j.finished - j.started for j in finished) / len(finished)) if finished else None,
                "done_today": sum(1 for j in self.jobs.values() if j.state == "done" and j.finished
                                  and date.fromtimestamp(j.finished) == today),
                "failed_today": sum(1 for j in self.jobs.values() if j.state == "error" and j.finished
                                    and date.fromtimestamp(j.finished) == today),
            },
            "ingest": {"auto": cfg["auto"], "folder": (self.library_folder() or Path("")).name or None},
            "alerts": alerts.summary(self.posts_dir),
        }

    def _review_alerts(self, job: Job, state: str) -> None:
        """Сигнал, когато писането на насрочен/взет от папката пост не мине или носи риск."""
        path = self.posts_dir / job.post_id
        if not path.is_dir() or job.kind == "repick":
            return
        web = _read_web(path)
        if not (web.get("scheduled_for") or web.get("source") == "folder" or web.get("batch")):
            return  # ръчен пост в чата — човекът гледа екрана, не му трябва сигнал
        if state == "error":
            self.alert("writing_failed", "error", f"{job.error or 'Агентът не завърши.'} Отворете поста и натиснете „Опитай пак“.",
                       post_id=job.post_id)
            return
        if state != "done":
            return
        item = next((p for p in self.list_posts() if p["id"] == job.post_id), None)
        if item is None:
            return
        view = self._post_view(item, {}, [])
        if any(p["kind"] == "time" for p in view["issues"]) and not web.get("auto_time_fix") and job.kind == "generate":
            # „вчера/утре" в пост за друг ден — агентът сам го поправя веднъж (лека редакция, ~30 s);
            # човекът пак одобрява. Сигнал — само ако и след това остане.
            web["auto_time_fix"] = round(time.time())
            write_json(path / "web.json", web)
            self._append_message(path, {"role": "agent", "kind": "note", "text": (
                f"Текстът казва „вчера/утре…“, а излиза на {item['scheduled_for']} — поправям го сам с конкретни дати.")})
            try:
                self.revise(job.post_id, (
                    "Смени думите за време, които зависят от деня на четене (днес, утре, вчера, тази седмица и т.н.), "
                    f"с конкретни дати. Постът излиза на {item['scheduled_for']}. Не пипай нищо друго."))
                return
            except HttpError:
                pass  # пълна опашка/зает — остава за човека, със сигнал по-долу
        serious = [p for p in view["issues"] if p["level"] in ("error", "warn") and p["kind"] != "stale"]
        if serious:
            self.alert("needs_review", serious[0]["level"], " · ".join(p["text"] for p in serious), post_id=job.post_id)
        else:
            alerts.resolve_for_post(self.posts_dir, job.post_id, REVIEW_ALERT_KINDS)

    # ---------------------------------------------------------------- сигнали

    def alert(self, kind: str, level: str, message: str, *, post_id: str | None = None, key: str | None = None) -> dict[str, Any]:
        title = None
        if post_id:
            path = self.posts_dir / post_id
            web = _read_web(path) if path.is_dir() else {}
            title = web.get("title") or _brief_title(read_text_or_none(path / "briefing.md") or "") or post_id
        return alerts.raise_alert(
            self.posts_dir, kind=kind, level=level, message=message, key=key or f"{kind}:{post_id}",
            post_id=post_id, post_title=title, sender=self.alert_sender,
        )

    def start_scheduler(self) -> None:
        def loop() -> None:
            last_ingest = 0.0
            while not self._stop.wait(SCHEDULER_SECONDS):
                try:
                    self.scheduler_tick()
                    if time.monotonic() - last_ingest >= INGEST_SECONDS:
                        last_ingest = time.monotonic()
                        self.ingest_tick()
                except Exception:  # noqa: BLE001 — проверката не бива да спира сървъра
                    traceback.print_exc()

        threading.Thread(target=loop, name="scheduler", daemon=True).start()

    # ---------------------------------------------------------------- папката на компютъра (web-ui-3)

    def library_folder(self) -> Path | None:
        return library.get_folder(self.posts_dir)

    def library_scan(self) -> dict[str, Any] | None:
        folder = self.library_folder()
        return library.scan(folder) if folder else None

    def _copy_album(self, path: Path, folder: Path, rel: str) -> int:
        try:
            images = library.album_images(folder, rel)
        except library.LibraryError as exc:
            raise HttpError(400, str(exc)) from exc
        names = []
        for index, image in enumerate(images):
            name = uploads.copy_in(path, image)
            if index >= uploads.MAX_AGENT_PHOTOS:
                uploads.set_included(path, name, False)  # над тавана — качени, но изключени
            else:
                names.append(name)
        self.add_pending(path, names)
        web = _read_web(path)
        web["album"] = rel
        write_json(path / "web.json", web)
        return len(images)

    def attach_album(self, post_id: str, rel: str) -> int:
        path = self.post_dir(post_id)
        self._ensure_idle_for(post_id)
        folder = self.library_folder()
        if folder is None:
            raise HttpError(409, "Първо изберете папка в „Папка“.")
        return self._copy_album(path, folder, rel)

    def library_schedule(self, rel: str) -> dict[str, Any]:
        """Чете файл с график направо от папката — без качване — и казва колко снимки намира на ред."""
        folder = self.library_folder()
        if folder is None:
            raise HttpError(409, "Първо изберете папка в „Папка“.")
        try:
            target = library.resolve(folder, rel)
            if not target.is_file() or target.stat().st_size > MAX_SCHEDULE_BYTES:
                raise HttpError(400, "Няма такъв файл или е твърде голям.")
            parsed = schedule.parse_schedule(target.name, target.read_bytes())
        except (library.LibraryError, schedule.ScheduleError) as exc:
            raise HttpError(400, str(exc)) from exc
        except OSError as exc:
            raise HttpError(400, "Не мога да прочета файла — отворен ли е в Excel?") from exc
        rows = []
        for row in parsed.rows:
            photos = library.resolve_photos(folder, row.photos, row.date)
            rows.append({**row.to_dict(), "photo_count": len(photos),
                         "photo_names": [p.name for p in photos[:6]]})
        return {"rows": rows, "warnings": parsed.warnings, "source": rel}

    def attach_batch_photos(self, batch: dict[str, Any]) -> None:
        """След create_batch от папката: снимките на всеки ред — копирани от диска, без качване."""
        folder = self.library_folder()
        if folder is None:
            return
        for item in batch.get("items", []):
            path = self.posts_dir / item["post_id"]
            for photo in library.resolve_photos(folder, item.get("photos") or [], item.get("date")):
                name = uploads.copy_in(path, photo)
                if uploads.included_count(path) > uploads.MAX_AGENT_PHOTOS:
                    uploads.set_included(path, name, False)

    # ---------------------------------------------------------------- асистент (web-ui-3)

    def _assistant_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import dataclasses

        from src.llm.client import LLMClient

        try:
            config = load_config()
        except ConfigError as exc:
            raise HttpError(503, str(exc)) from exc
        # Разговорът трябва да отговаря за секунди, не за минути: само ТОЗИ клиент пита модела
        # без скрити разсъждения (конвейерът за писане е в отделен процес и не се засяга).
        # Измерено 2026-10-14: план за месец с thinking — 2:35 мин. ASSISTANT_THINKING=1 го връща.
        if os.environ.get("ASSISTANT_THINKING", "").strip().lower() not in ("1", "true", "да"):
            config = dataclasses.replace(config, no_thinking_roles=frozenset(config.no_thinking_roles) | {"planner"})
        return LLMClient(config)

    def assistant_message(self, thread_id: str, text: str, mode: str | None = None) -> dict[str, Any]:
        from src.errors import AgentError

        client = self._assistant_client()
        try:
            thread = assistant.respond(
                self.posts_dir, thread_id, text,
                client=client, calendar=self.list_posts(), library_scan=self.library_scan(), mode=mode,
            )
        except assistant.AssistantError as exc:
            raise HttpError(409, str(exc)) from exc
        except AgentError as exc:
            raise HttpError(502, exc.human_message) from exc
        last = thread["messages"][-1]
        if last.get("write_now"):
            # Асистентът и „нов пост" са едно място: описано събитие → постът се пише веднага.
            last["post_id"] = self._write_from_plan(last["plan"][0], text)
            assistant.save_thread(self.posts_dir, thread)
        return thread

    def _write_from_plan(self, item: dict[str, Any], message: str) -> str:
        post_id = self.create_post(item["brief"], item["mode"])
        path = self.posts_dir / post_id
        folder = self.library_folder()
        if item.get("album") and folder:
            self._copy_album(path, folder, item["album"])
        if item["date"] > date.today().isoformat():
            web = _read_web(path)
            web.update({"scheduled_for": item["date"], "scheduled_time": item["time"]})
            write_json(path / "web.json", web)
        try:
            job = self.generate(post_id, item["brief"], item["mode"], message=message)
        except HttpError as exc:
            # Пълна опашка и т.н. — постът стои с брифа; човекът го пуска от чата му.
            self._append_message(path, {"role": "agent", "kind": "error", "text": exc.message})
            return post_id
        return job.post_id

    def apply_plan(self, thread_id: str, index: int, items: list[dict[str, Any]], name: str | None) -> dict[str, Any]:
        """Одобреното от човека предложение → постове в календара; тези с бриф тръгват да се пишат."""
        try:
            thread = assistant.load_thread(self.posts_dir, thread_id)
        except assistant.AssistantError as exc:
            raise HttpError(404, str(exc)) from exc
        folder = self.library_folder()
        album_list = library.scan(folder)["albums"] if folder else []
        # Празен бриф + албум с текст → текстът от папката (написан от човека) е брифът (clean_plan).
        albums = {a["path"]: a.get("brief") for a in album_list}
        plan, _ = assistant.clean_plan(items, date.today(), albums)
        if not plan:
            raise HttpError(400, "В плана няма нито един ред с бъдеща дата.")
        batch = batches.make_batch(_plan_name(plan), f"Асистент: {thread.get('title') or ''}"[:200], [])
        to_write = []
        for row, item in enumerate(plan, start=1):
            post_id = self.create_post(item["brief"], item["mode"])
            path = self.posts_dir / post_id
            web = _read_web(path)
            web.update({
                "auto_name": False, "batch": batch["id"], "title": item["title"],
                "scheduled_for": item["date"], "scheduled_time": item["time"],
            })
            if item["needs_brief"]:
                web["needs_brief"] = True
                if item.get("idea"):
                    web["idea"] = item["idea"]
            write_json(path / "web.json", web)
            if item["album"] and folder:
                self._copy_album(path, folder, item["album"])
            batch["items"].append({
                "post_id": post_id, "row": row, "date": item["date"], "title": item["title"],
                "photos": [item["album"]] if item["album"] else [],
            })
            if not item["needs_brief"]:
                to_write.append(post_id)
        batches.save_batch(self.posts_dir, batch)
        if to_write:
            self.start_batch(batch["id"], to_write)
        assistant.mark_applied(self.posts_dir, thread_id, index, batch["id"], len(plan))
        return {"batch": self.batch_status(batch["id"]), "thread": assistant.load_thread(self.posts_dir, thread_id)}

    def home(self) -> dict[str, Any]:
        posts = self.list_posts()
        scan = self.library_scan()
        today = date.today().isoformat()
        return {
            "suggestions": insights.suggestions(posts, scan, today),
            "folder": scan["name"] if scan else None,
            "upcoming": sorted(
                (p for p in posts if (p.get("scheduled_for") or "") >= today),
                key=lambda p: (p["scheduled_for"], p.get("scheduled_time") or ""),
            )[:6],
        }


_MONTHS_SHORT = ("яну", "фев", "мар", "апр", "май", "юни", "юли", "авг", "сеп", "окт", "ное", "дек")


def _plan_name(plan: list[dict[str, Any]]) -> str:
    """„3 поста · 21 окт – 4 ное" — кратко име за историята, не цялото съобщение на човека."""
    def short(iso: str) -> str:
        y, m, d = iso.split("-")
        return f"{int(d)} {_MONTHS_SHORT[int(m) - 1]}"

    count = f"{len(plan)} {'пост' if len(plan) == 1 else 'поста'}"
    first, last = plan[0]["date"], plan[-1]["date"]
    return f"{count} · {short(first)}" + (f" – {short(last)}" if last != first else "")


def _written_on(path: Path) -> str | None:
    """Денят, в който е написан текстът, който човекът вижда (ръчна редакция или предложението)."""
    for name in ("edited.md", "post.md"):
        target = path / "output" / name
        if target.exists():
            return datetime.fromtimestamp(target.stat().st_mtime).date().isoformat()
    return None


def _text_hash(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


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
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
            "modes": insights.mode_options(self.app.posts_dir),
            "default_mode": modes.DEFAULT_MODE,
            "config_error": self.app.config_problem(),
            "max_agent_photos": uploads.MAX_AGENT_PHOTOS,
            "max_jobs": self.app.max_jobs,
            "jobs": [job.to_dict() for job in self.app.running_jobs()],
            "queued": len(self.app.queue),
            "publish_mode": settings.mode,
            "publish_problem": settings.problem(),
            "alerts": alerts.summary(self.app.posts_dir),
            "alert_channels": alerts.channels_from_env() if self.app.alert_sender else {"webhook": False, "email": False},
            "tutorial": self.app.tutorial_state(),
            "ui_version": UI_VERSION,
        })

    def r_settings(self, *, query) -> None:
        self._json(self.app.settings_view())

    def r_tutorial_set(self, *, query) -> None:
        data = self._read_json()
        self._json(self.app.set_tutorial(str(data.get("state") or ""), data.get("step")))

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
        if data.get("library"):
            self.app.attach_batch_photos(batch)  # снимките идват направо от папката на компютъра
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
    # ---------------------------------------------------------------- web-ui-3: папка, асистент, насрочване

    def _local_only(self) -> None:
        """Изборът на папка разглежда диска — само от самия компютър, никога от мрежата."""
        if not _is_loopback(self.client_address[0]):
            raise HttpError(403, "Папката се избира само от компютъра, на който работи агентът.")

    def r_home(self, *, query) -> None:
        self._json(self.app.home())

    def r_library(self, *, query) -> None:
        scan = self.app.library_scan()
        self._json({"library": scan, "can_choose": _is_loopback(self.client_address[0])})

    def r_library_set(self, *, query) -> None:
        self._local_only()
        data = self._read_json()
        try:
            library.set_folder(self.app.posts_dir, str(data.get("folder") or "").strip() or None)
        except library.LibraryError as exc:
            raise HttpError(400, str(exc)) from exc
        insights.clear_cache()
        self._json({"library": self.app.library_scan(), "can_choose": True})

    def r_library_browse(self, *, query) -> None:
        self._local_only()
        self._json(library.browse((query.get("path") or [""])[0] or None))

    def r_library_image(self, *, query) -> None:
        folder = self.app.library_folder()
        if folder is None:
            raise HttpError(404, "Няма избрана папка.")
        try:
            source = library.resolve(folder, (query.get("path") or [""])[0])
        except library.LibraryError as exc:
            raise HttpError(404, str(exc)) from exc
        thumb = library.thumbnail(source, self.app.posts_dir / library.THUMBS_DIR, (query.get("size") or ["s"])[0])
        if thumb is None:
            raise HttpError(404, "Няма такава снимка.")
        self._file(thumb, "image/jpeg", "private, max-age=3600")

    def r_library_schedule(self, *, query) -> None:
        data = self._read_json()
        self._json(self.app.library_schedule(str(data.get("path") or "")))

    def r_library_album(self, *, query) -> None:
        data = self._read_json()
        rel = str(data.get("path") or "")
        post_id = self.app.create_post("", data.get("mode"))
        path = self.app.posts_dir / post_id
        web = _read_web(path)
        web["title"] = (rel.rsplit("/", 1)[-1] or "Албум")[:120]
        write_json(path / "web.json", web)
        self.app.attach_album(post_id, rel)
        state = self.app.post_state(post_id)
        folder = self.app.library_folder()
        album = next((a for a in library.scan(folder)["albums"] if a["path"] == rel), None) if folder else None
        state["album_brief"] = album.get("brief") if album else None  # текстът от папката → в полето за писане
        self._json(state, 201)

    def r_post_album(self, post_id: str, *, query) -> None:
        data = self._read_json()
        count = self.app.attach_album(post_id, str(data.get("path") or ""))
        self._json({"added": count, **self.app.post_state(post_id)})

    def r_schedule_set(self, post_id: str, *, query) -> None:
        data = self._read_json()
        info = self.app.set_schedule(
            post_id, when=data.get("date"), at=data.get("time"),
            approved=data.get("approved") if "approved" in data else None,
        )
        self._json(info)

    def r_threads(self, *, query) -> None:
        self._json({"threads": assistant.list_threads(self.app.posts_dir)})

    def r_thread_create(self, *, query) -> None:
        self._read_json()
        self._json(assistant.create_thread(self.app.posts_dir), 201)

    def r_thread(self, thread_id: str, *, query) -> None:
        try:
            self._json(assistant.load_thread(self.app.posts_dir, thread_id))
        except assistant.AssistantError as exc:
            raise HttpError(404, str(exc)) from exc

    def r_thread_delete(self, thread_id: str, *, query) -> None:
        try:
            assistant.delete_thread(self.app.posts_dir, thread_id)
        except assistant.AssistantError as exc:
            raise HttpError(404, str(exc)) from exc
        self._json({"ok": True})

    def r_thread_message(self, thread_id: str, *, query) -> None:
        data = self._read_json()
        self._json(self.app.assistant_message(thread_id, str(data.get("text") or ""), data.get("mode")))

    def r_overview(self, *, query) -> None:
        self._json(self.app.overview())

    def r_ingest_set(self, *, query) -> None:
        data = self._read_json()
        self._json(self.app.set_auto_ingest(bool(data.get("auto"))))

    def r_ingest_now(self, *, query) -> None:
        data = self._read_json()
        paths = data.get("paths") if isinstance(data.get("paths"), list) else []
        created = [self.app.ingest_album(str(p)) for p in paths[:60]]
        self._json({"created": created}, 201)

    def r_inbox_post(self, *, query) -> None:
        data = self._read_json()
        self._json(self.app.inbox_post(str(data.get("name") or ""), str(data.get("text") or ""),
                                       str(data.get("batch") or "") or None), 201)

    def r_inbox_batch(self, *, query) -> None:
        data = self._read_json()
        self._json(self.app.inbox_batch(str(data.get("name") or "")), 201)

    def r_inbox_start(self, post_id: str, *, query) -> None:
        self._read_json()
        self._json(self.app.inbox_start(post_id))

    def r_rewrite_source(self, post_id: str, *, query) -> None:
        self._read_json()
        job = self.app.rewrite_from_source(post_id)
        self._json({"job": job.to_dict(), "post_id": job.post_id})

    def r_alerts(self, *, query) -> None:
        self._json({"alerts": alerts.list_alerts(self.app.posts_dir)})

    def r_alert_resolve(self, alert_id: str, *, query) -> None:
        self._read_json()
        if not alerts.resolve(self.app.posts_dir, alert_id):
            raise HttpError(404, "Няма такъв отворен сигнал.")
        self._json(alerts.summary(self.app.posts_dir))

    def r_alert_test(self, *, query) -> None:
        self._read_json()
        alert = self.app.alert("test", "action",
                               "Проба: ако виждате това в Teams/Slack/имейл, сигналите работят.",
                               key=f"test:{uuid.uuid4().hex[:6]}")
        self._json({"alert": alert, "sending": bool(self.app.alert_sender)}, 201)

    def r_thread_apply(self, thread_id: str, *, query) -> None:
        data = self._read_json()
        items = data.get("items") if isinstance(data.get("items"), list) else []
        self._json(self.app.apply_plan(thread_id, int(data.get("index") or 0), items, data.get("name")), 201)


_P = r"([^/]+)"
_ROUTES: list[tuple[str, re.Pattern[str], Callable[..., None]]] = [
    ("GET", re.compile(r"/"), _Handler.r_index),
    ("GET", re.compile(r"/static/([A-Za-z0-9._-]+)"), _Handler.r_static),
    ("GET", re.compile(r"/api/info"), _Handler.r_info),
    ("GET", re.compile(r"/api/settings"), _Handler.r_settings),
    ("PUT", re.compile(r"/api/settings/tutorial"), _Handler.r_tutorial_set),
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
    ("GET", re.compile(r"/api/home"), _Handler.r_home),
    ("GET", re.compile(r"/api/library"), _Handler.r_library),
    ("PUT", re.compile(r"/api/library"), _Handler.r_library_set),
    ("GET", re.compile(r"/api/library/browse"), _Handler.r_library_browse),
    ("GET", re.compile(r"/api/library/image"), _Handler.r_library_image),
    ("POST", re.compile(r"/api/library/schedule"), _Handler.r_library_schedule),
    ("POST", re.compile(r"/api/library/album"), _Handler.r_library_album),
    ("POST", re.compile(rf"/api/posts/{_P}/album"), _Handler.r_post_album),
    ("PUT", re.compile(rf"/api/posts/{_P}/schedule"), _Handler.r_schedule_set),
    ("GET", re.compile(r"/api/assistant"), _Handler.r_threads),
    ("POST", re.compile(r"/api/assistant"), _Handler.r_thread_create),
    ("GET", re.compile(rf"/api/assistant/{_P}"), _Handler.r_thread),
    ("DELETE", re.compile(rf"/api/assistant/{_P}"), _Handler.r_thread_delete),
    ("POST", re.compile(rf"/api/assistant/{_P}/messages"), _Handler.r_thread_message),
    ("POST", re.compile(rf"/api/assistant/{_P}/apply"), _Handler.r_thread_apply),
    ("GET", re.compile(r"/api/alerts"), _Handler.r_alerts),
    ("GET", re.compile(r"/api/overview"), _Handler.r_overview),
    ("PUT", re.compile(r"/api/ingest"), _Handler.r_ingest_set),
    ("POST", re.compile(r"/api/ingest"), _Handler.r_ingest_now),
    ("POST", re.compile(r"/api/inbox"), _Handler.r_inbox_post),
    ("POST", re.compile(r"/api/inbox/batch"), _Handler.r_inbox_batch),
    ("POST", re.compile(rf"/api/inbox/{_P}/start"), _Handler.r_inbox_start),
    ("POST", re.compile(rf"/api/posts/{_P}/rewrite-source"), _Handler.r_rewrite_source),
    ("POST", re.compile(r"/api/alerts/test"), _Handler.r_alert_test),
    ("POST", re.compile(rf"/api/alerts/{_P}/resolve"), _Handler.r_alert_resolve),
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


class _ExclusiveServer(ThreadingHTTPServer):
    """
    Порт само за един процес. На Windows `allow_reuse_address` (подразбирането на http.server)
    значи SO_REUSEADDR — и ВТОРИ сървър тихо заема същия порт, а заявките продължават към стария.
    Видяно на живо 2026-10-14: стар процес от 15:43 отговаряше, „рестартът не помагаше“. С
    изключителен порт вторият старт гърми и казва защо (виж serve()).
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    server = _ExclusiveServer((host, port), _Handler)
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

    app = WebApp(posts_dir, password=password, max_jobs=max_jobs, alert_sender=alerts.sender_from_env())
    try:
        server = make_server(app, host, port)
        app.start_scheduler()
    except OSError as exc:
        app.close()
        print(
            f"Порт {port} е зает — вероятно агентът вече работи в друг прозорец. Затворете всички черни "
            f"прозорци на агента и пуснете start_web.bat отново (той сам спира старите). "
            f"Техническо: {exc}"
        )
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
