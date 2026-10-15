"""
Потокът „пускам папка → получавам готов пост" с един поглед (docs/technical/web-ui-4.md).

  parse_header()  — в текстовия файл на пост-папка човекът може да напише най-отгоре:
                       Кога: 21.10.2026 10:00
                       Режим: кадър
                       Заглавие: Хакатон „Код за Варна“
                       ---            (по желание — край на заглавните редове)
                    Останалото е брифът. Нищо от това не е задължително.
  when_from()     — датата/часът: от „Кога:“, иначе от името на папката
                    („2026-10-21 10.00 Хакатон“, „21.10 Хакатон“).
  issues()        — защо един готов пост иска човешки поглед, на човешки език, с действие.
  lane()          — в коя колона на „Преглед“ е постът.

Само чисти функции — състоянието е във файловете на поста (server.py ги чете).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from src.humanize import humanize_note
from src.pipeline import modes
from src.web import schedule

_HEADER = re.compile(r"^\s*(кога|дата|час|режим|дължина|заглавие|title|date|time|mode)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")  # „10:00" — с двоеточие, иначе „21.10" е дата
_TIME_DOT = re.compile(r"\b([01]?\d|2[0-3])\.([0-5]\d)\s*ч")  # „10.00 ч." — точка само с „ч"
_TIME_LOOSE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")  # в реда „Час:" няма дата — точката е ОК
_NAME_TIME = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}[._]\d{1,2}(?:[._]\d{2,4})?)[\s_-]+([01]?\d|2[0-3])[.:h]([0-5]\d)\b")
_NAME_PREFIX = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}[._]\d{1,2}(?:[._]\d{2,4})?)(?:[\s_-]+(?:[01]?\d|2[0-3])[.:h][0-5]\d)?[\s_–-]*")


def parse_header(text: str) -> tuple[dict[str, str], str]:
    """Заглавните редове най-отгоре → (стойности, брифът без тях)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    meta: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            if meta:
                index += 1
                break
            index += 1
            continue
        if line.strip() in ("---", "—", "–––"):
            index += 1
            break
        match = _HEADER.match(line)
        if not match:
            break
        key = match.group(1).lower()
        key = {"дата": "кога", "date": "кога", "time": "час", "mode": "режим", "дължина": "режим", "title": "заглавие"}.get(key, key)
        meta[key] = match.group(2)
        index += 1
    body = "\n".join(lines[index:]).strip() if meta else (text or "").strip()
    return meta, body


def when_from(folder_name: str, meta: dict[str, str], *, today: date | None = None) -> tuple[str | None, str | None]:
    """(ISO дата, „HH:MM") — от „Кога:"/„Час:", иначе от името на папката. Липсващото е None."""
    when = None
    at = None
    raw = meta.get("кога") or ""
    if raw:
        match = _TIME.search(raw) or _TIME_DOT.search(raw)
        if match:
            at = f"{int(match.group(1)):02d}:{match.group(2)}"
        date_part = (raw[: match.start()] + raw[match.end():]) if match else raw
        date_part = re.sub(r"(^|\s)(в|от|ч\.?)(?=\s|$)", " ", date_part).strip(" ,-–")
        first = date_part.split()[0] if date_part.split() else ""
        when = schedule.parse_date(date_part, today=today) or schedule.parse_date(first, today=today)
    if meta.get("час") and not at:
        match = _TIME_LOOSE.search(meta["час"])
        if match:
            at = f"{int(match.group(1)):02d}:{match.group(2)}"
    if not when:
        from src.web.library import date_from_name  # кръгов import: library → schedule, не flow

        when = date_from_name(folder_name)
    if not at:
        match = _NAME_TIME.match(folder_name)
        if match:
            at = f"{int(match.group(1)):02d}:{match.group(2)}"
    return when, at


def title_from(folder_name: str, meta: dict[str, str]) -> str:
    """„2026-10-21 10.00 Хакатон" → „Хакатон"; „Заглавие:" печели."""
    if meta.get("заглавие"):
        return " ".join(meta["заглавие"].split())[:120]
    stripped = _NAME_PREFIX.sub("", folder_name).strip(" -–_")
    return (stripped or folder_name)[:120]


def mode_from(meta: dict[str, str]) -> str | None:
    return modes.resolve_mode(meta.get("режим") or "")


# --------------------------------------------------------------------------- внимание


def issues(
    *,
    has_text: bool,
    meta: dict[str, Any],
    sched: dict[str, Any],
    job_error: str | None,
    source_changed: bool,
    published: bool,
    overdue: bool = False,
    publish_mode: str = "draft",
) -> list[dict[str, str]]:
    """
    Причините, поради които постът иска човек — подредени по тежест. Всяка има `kind`
    (за действие в интерфейса) и `text` (за човека). Бележките на Критика са само брой —
    те са в поста; тук влиза това, което спира публикуването или е риск.
    """
    out: list[dict[str, str]] = []
    if published:
        return out
    if job_error and not has_text:
        out.append({"kind": "failed", "level": "error", "text": f"Писането не мина: {job_error}"})
        return out
    if not has_text:
        if sched.get("needs_brief"):
            out.append({"kind": "brief", "level": "warn", "text": "Още няма какво да се пише — нужен е бриф."})
        return out
    if overdue:
        out.append({"kind": "due", "level": "error" if publish_mode == "linkedin" else "warn",
                    "text": ("Часът мина, а постът не е публикуван — вижте „Сигнали“." if publish_mode == "linkedin"
                             else "Часът дойде — публикувайте го в LinkedIn и натиснете „Публикувах го“.")})
    if meta.get("insufficient_brief"):
        out.append({"kind": "thin", "level": "error", "text": "Брифът не стига за истински пост — допишете подробности."})
    if sched.get("time_warning"):
        out.append({"kind": "time", "level": "warn", "text": sched["time_warning"]})
    if source_changed:
        out.append({"kind": "source", "level": "warn", "text": "Текстът в папката е променен след писането — да пиша ли наново?"})
    if sched.get("approval_stale"):
        out.append({"kind": "stale", "level": "warn", "text": "Текстът е променен след одобрението — одобрете пак."})
    digest = note_digest(meta)
    if digest and meta.get("status") != "ready":
        out.append({"kind": "notes", "level": "info", "text": digest})
    return out


# Коя бележка е най-важна за човека — първата съвпаднала група печели (research-plan-38).
_NOTE_PRIORITY = ("ЧУВСТВИТЕЛНА", "[GDPR]", "rules/forbidden", "Недоказано твърдение", "Хеджирано", "Media Picker")


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def note_digest(meta: dict[str, Any]) -> str:
    """
    Едно изречение вместо „13 бележки от проверката": най-важната бележка (на човешки език,
    src/humanize.py) + колко още, и колко агентът е махнал сам след проверката (стъпка [8]).
    Празно, ако няма нищо.
    """
    raw = [str(n) for n in list(meta.get("warnings", [])) + list(meta.get("open_notes", []))]
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for note in raw:
        human = humanize_note(note)
        if human and human not in seen:
            seen.add(human)
            pairs.append((note, human))
    fixed = sum(1 for f in meta.get("applied_fixes", []) or [] if str(f).startswith("Махнато след проверката"))
    parts: list[str] = []
    if pairs:
        top = next(
            (human for marker in _NOTE_PRIORITY for note, human in pairs if marker in note),
            pairs[0][1],
        )
        rest = len(pairs) - 1
        parts.append(_short(top) + (f" (+{rest} {'бележка' if rest == 1 else 'бележки'})" if rest else ""))
    if fixed:
        parts.append(f"Агентът махна сам {fixed} {'недоказано твърдение' if fixed == 1 else 'недоказани твърдения'}.")
    return " · ".join(parts)


def lane(*, running: bool, queued: bool, has_text: bool, published: bool, problems: list[dict[str, str]],
         needs_brief: bool) -> str:
    if published:
        return "published"
    if running or queued:
        return "writing"
    if not has_text:
        return "review" if any(p["kind"] == "failed" for p in problems) else "inbox"
    if any(p["level"] in ("error", "warn") for p in problems):
        return "review"
    return "ready"
