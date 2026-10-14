"""
Сигнали — когато нещо с публикуването в LinkedIn не е наред (docs/technical/web-ui-3.md).

Кога се вдига сигнал:
  publish_failed   публикуването (ръчно или по график) върна грешка           — грешка
  publish_missed   часът мина, докато агентът беше спрян (> 6 ч.)              — грешка
  publish_config   одобрен пост е насрочен до 24 ч., а настройките за LinkedIn
                   липсват/са грешни (няма токен, грешен ORG_ID)              — предупреждение
  token_expiring   LINKEDIN_TOKEN_EXPIRES е след ≤ 7 дни (или вече мина)        — предупреждение
  due_reminder     режим „чернова“: часът дойде — публикувайте ръчно            — за действие

Къде се вижда:
  - в интерфейса: камбанка в лявата колона с брой, изглед „Сигнали“ (#/signali), известие;
  - навън (по избор, само ако е настроено в .env):
      ALERT_WEBHOOK_URL   Teams/Slack „Incoming webhook“ — POST {"text": …}
      ALERT_EMAIL_TO      имейл през SMTP (ALERT_SMTP_HOST/PORT/USER/PASSWORD, ALERT_EMAIL_FROM)

Файловата система е състоянието: posts/_alerts.json. Един и същ проблем не се дублира —
отворен сигнал със същия ключ само увеличава брояча. Успешно публикуване затваря сигналите
на поста. Изпращането навън е в отделна нишка — бавен SMTP не спира сървъра.
Само стандартна библиотека (виж server.py защо).
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
import traceback
import urllib.request
import uuid
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from src.pipeline.util import read_json, write_json

ALERTS_FILE = "_alerts.json"
MAX_KEEP = 300
LEVELS = ("error", "warn", "action")
KIND_LABELS = {
    "publish_failed": "Публикуването не мина",
    "publish_missed": "Пропуснат час за публикуване",
    "publish_config": "Настройките за LinkedIn не са готови",
    "token_expiring": "Токенът за LinkedIn изтича",
    "due_reminder": "Време е за публикуване",
    "writing_failed": "Писането на пост не мина",
    "needs_review": "Пост иска преглед",
    "test": "Тестов сигнал",
}

_lock = threading.Lock()

Sender = Callable[[dict[str, Any]], dict[str, str]]


# --------------------------------------------------------------------------- съхранение


def _path(posts_dir: Path) -> Path:
    return posts_dir / ALERTS_FILE


def _load(posts_dir: Path) -> list[dict[str, Any]]:
    try:
        data = read_json(_path(posts_dir))
    except (OSError, ValueError):
        return []
    return data.get("alerts", []) if isinstance(data, dict) else []


def _save(posts_dir: Path, alerts: list[dict[str, Any]]) -> None:
    # Отворените винаги остават; от решените — само най-новите.
    open_ = [a for a in alerts if not a.get("resolved")]
    closed = sorted((a for a in alerts if a.get("resolved")), key=lambda a: a.get("t", 0), reverse=True)
    kept = open_ + closed[: max(0, MAX_KEEP - len(open_))]
    posts_dir.mkdir(parents=True, exist_ok=True)
    write_json(_path(posts_dir), {"alerts": kept})


def list_alerts(posts_dir: Path, *, include_resolved: bool = True) -> list[dict[str, Any]]:
    alerts = _load(posts_dir)
    if not include_resolved:
        alerts = [a for a in alerts if not a.get("resolved")]
    return sorted(alerts, key=lambda a: (bool(a.get("resolved")), -a.get("t", 0)))


def summary(posts_dir: Path) -> dict[str, Any]:
    open_ = [a for a in _load(posts_dir) if not a.get("resolved")]
    latest = max(open_, key=lambda a: a.get("t", 0)) if open_ else None
    return {
        "open": len(open_),
        "errors": sum(1 for a in open_ if a.get("level") == "error"),
        "latest": {k: latest.get(k) for k in ("id", "level", "title", "message", "post_id")} if latest else None,
    }


def raise_alert(
    posts_dir: Path,
    *,
    kind: str,
    level: str,
    message: str,
    key: str,
    post_id: str | None = None,
    post_title: str | None = None,
    sender: Sender | None = None,
) -> dict[str, Any]:
    """Вдига сигнал (или увеличава брояча на отворен със същия `key`) и го праща навън."""
    level = level if level in LEVELS else "warn"
    now = round(time.time())
    with _lock:
        alerts = _load(posts_dir)
        existing = next((a for a in alerts if a.get("key") == key and not a.get("resolved")), None)
        if existing:
            existing["count"] = existing.get("count", 1) + 1
            existing["last"] = now
            existing["message"] = message
            _save(posts_dir, alerts)
            return existing  # същият проблем не се праща навън втори път
        alert = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "level": level,
            "title": KIND_LABELS.get(kind, kind),
            "message": message,
            "key": key,
            "post_id": post_id,
            "post_title": post_title,
            "t": now,
            "last": now,
            "count": 1,
            "resolved": False,
            "delivery": {},
        }
        alerts.append(alert)
        _save(posts_dir, alerts)
    if sender is not None:
        threading.Thread(target=_deliver, args=(posts_dir, alert, sender), name="alert", daemon=True).start()
    return alert


def _deliver(posts_dir: Path, alert: dict[str, Any], sender: Sender) -> None:
    try:
        delivery = sender(alert)
    except Exception as exc:  # noqa: BLE001 — сигналът остава в интерфейса, каквото и да стане навън
        traceback.print_exc()
        delivery = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    with _lock:
        alerts = _load(posts_dir)
        for item in alerts:
            if item.get("id") == alert["id"]:
                item["delivery"] = delivery
        _save(posts_dir, alerts)


def resolve(posts_dir: Path, alert_id: str, *, by: str = "човек") -> bool:
    with _lock:
        alerts = _load(posts_dir)
        hit = False
        for item in alerts:
            if item.get("id") == alert_id and not item.get("resolved"):
                item["resolved"] = round(time.time())
                item["resolved_by"] = by
                hit = True
        if hit:
            _save(posts_dir, alerts)
        return hit


def resolve_for_post(posts_dir: Path, post_id: str, kinds: tuple[str, ...], *, by: str = "агент") -> int:
    """Постът е публикуван → неговите сигнали за публикуване вече не са актуални."""
    with _lock:
        alerts = _load(posts_dir)
        count = 0
        for item in alerts:
            if item.get("post_id") == post_id and item.get("kind") in kinds and not item.get("resolved"):
                item["resolved"] = round(time.time())
                item["resolved_by"] = by
                count += 1
        if count:
            _save(posts_dir, alerts)
        return count


def resolve_by_key(posts_dir: Path, key: str) -> None:
    with _lock:
        alerts = _load(posts_dir)
        changed = False
        for item in alerts:
            if item.get("key") == key and not item.get("resolved"):
                item["resolved"] = round(time.time())
                item["resolved_by"] = "агент"
                changed = True
        if changed:
            _save(posts_dir, alerts)


# --------------------------------------------------------------------------- навън


def channels_from_env() -> dict[str, Any]:
    """Кои канали са настроени (без тайни — за показване в интерфейса)."""
    webhook = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    smtp = os.environ.get("ALERT_SMTP_HOST", "").strip()
    return {
        "webhook": bool(webhook),
        "webhook_host": urllib.request.urlparse(webhook).hostname if webhook else None,
        "email": bool(email_to and smtp),
        "email_to": email_to or None,
    }


def _text(alert: dict[str, Any]) -> str:
    icon = {"error": "🔴", "warn": "🟠", "action": "🔵"}.get(alert.get("level"), "•")
    post = f"\nПост: {alert['post_title']}" if alert.get("post_title") else ""
    return f"{icon} LinkedIn агент — {alert['title']}{post}\n{alert['message']}"


def sender_from_env() -> Sender | None:
    """Изпращач според .env; None — нищо не е настроено (сигналите остават само в интерфейса)."""
    webhook = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    host = os.environ.get("ALERT_SMTP_HOST", "").strip()
    if not webhook and not (email_to and host):
        return None

    def send(alert: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        text = _text(alert)
        if webhook:
            try:
                body = json.dumps({"text": text}).encode("utf-8")
                request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 — адресът е от .env
                    result["webhook"] = f"ok {response.status}"
            except Exception as exc:  # noqa: BLE001
                result["webhook"] = f"грешка: {exc}"[:200]
        if email_to and host:
            try:
                message = EmailMessage()
                message["Subject"] = f"LinkedIn агент: {alert['title']}"
                message["From"] = os.environ.get("ALERT_EMAIL_FROM", "").strip() or os.environ.get("ALERT_SMTP_USER", "").strip()
                message["To"] = email_to
                message.set_content(text)
                port = int(os.environ.get("ALERT_SMTP_PORT", "").strip() or 587)
                with smtplib.SMTP(host, port, timeout=20) as smtp:
                    if port != 25:
                        smtp.starttls(context=ssl.create_default_context())
                    user = os.environ.get("ALERT_SMTP_USER", "").strip()
                    if user:
                        smtp.login(user, os.environ.get("ALERT_SMTP_PASSWORD", ""))
                    smtp.send_message(message)
                result["email"] = "ok"
            except Exception as exc:  # noqa: BLE001
                result["email"] = f"грешка: {exc}"[:200]
        return result

    return send
