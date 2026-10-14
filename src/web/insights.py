"""
Живи подсказки за интерфейса (docs/technical/web-ui-3.md) — нищо твърдо зашито в браузъра:

  mode_options()  — какво е всеки режим: описание от modes.py, диапазон от думи (Фокус
                    чете своя направо от knowledge/voice/tone.md — промяна там се вижда
                    веднага), плюс жива статистика от ВАШИТЕ постове в този режим: колко,
                    средно думи, и началото на най-новия като пример.
  suggestions()   — предложения за начало, сглобени от това, което реално има: албуми в
                    папката без пост, празни места в календара, постове с бележки. Сменят
                    се сами, щом се появи нещо ново.

Само четене от файловата система; кеш за кратко, защото /api/info се вика на секунди.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from src.pipeline import modes
from src.pipeline.util import read_json, read_text_or_none

TONE_FILE = Path("knowledge") / "voice" / "tone.md"
# Думи, чийто смисъл зависи от деня, в който се ЧЕТАТ — вярни в деня на писане, грешни в деня
# на публикуване, ако двата са различни (web-ui-3: „пиши сега, публикувай по график").
_RELATIVE_TIME = re.compile(
    r"\b(днес|днешн\w*|утре|утрешн\w*|вдругиден|вчера|вчерашн\w*|завчера|снощи|тази сутрин|тази вечер|"
    r"тази седмица|миналата седмица|следващата седмица|този уикенд|миналия уикенд|този месец|"
    r"преди (?:няколко|два|три) дни|след (?:няколко|два|три) дни|в момента|току-що)\b",
    re.IGNORECASE,
)
_RANGE = re.compile(r"Дължина:\s*(\d+)\s*[–-]\s*(\d+)\s*думи")
_CACHE_SECONDS = 30
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, build):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
    value = build()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def corpus_range(tone_file: Path = TONE_FILE) -> tuple[int, int] | None:
    """Диапазонът по подразбиране от tone.md („Дължина: 100–400 думи") — None, ако го няма."""
    match = _RANGE.search(read_text_or_none(tone_file) or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def _final_text(path: Path) -> str:
    output = path / "output"
    return ((read_text_or_none(output / "edited.md") or "").strip()
            or (read_text_or_none(output / "post.md") or "").strip())


def _mode_stats(posts_dir: Path) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {key: {"count": 0, "words": [], "latest": None} for key in modes.MODES}
    if not posts_dir.is_dir():
        return stats
    for path in posts_dir.iterdir():
        if not path.is_dir() or path.name.startswith(("_", ".")):
            continue
        text = _final_text(path)
        if not text:
            continue
        try:
            mode = read_json(path / "web.json").get("mode") or modes.DEFAULT_MODE
        except (OSError, ValueError):
            mode = modes.DEFAULT_MODE
        bucket = stats.get(mode)
        if bucket is None:
            continue
        try:
            stamp = (path / "output" / "post.md").stat().st_mtime
        except OSError:
            stamp = 0
        bucket["count"] += 1
        bucket["words"].append(len(text.split()))
        if bucket["latest"] is None or stamp > bucket["latest"][0]:
            bucket["latest"] = (stamp, text)
    return stats


def mode_options(posts_dir: Path, tone_file: Path = TONE_FILE) -> list[dict[str, Any]]:
    def build() -> list[dict[str, Any]]:
        corpus = corpus_range(tone_file)
        stats = _mode_stats(posts_dir)
        result = []
        for m in modes.MODES.values():
            low, high = (m.words_min, m.words_max) if m.words_min else (corpus or (None, None))
            words = f"{low}–{high} думи" if low else "по корпуса"
            bucket = stats.get(m.key) or {"count": 0, "words": [], "latest": None}
            sample = None
            if bucket["latest"]:
                first = bucket["latest"][1].strip().split("\n", 1)[0]
                sample = first[:140] + ("…" if len(first) > 140 else "")
            avg = round(sum(bucket["words"]) / len(bucket["words"])) if bucket["words"] else None
            result.append({
                "key": m.key,
                "label": m.label,
                "hint": words,
                "words_min": low,
                "words_max": high,
                "best_for": m.best_for,
                # LinkedIn показва ~210 знака преди „…още"; ~6 знака на дума на български.
                "fits_fold": bool(high and high * 6.5 <= 600),
                "used": bucket["count"],
                "avg_words": avg,
                "sample": sample,
            })
        return result

    return _cached(f"modes:{posts_dir}", build)


def suggestions(
    posts: list[dict[str, Any]],
    library_scan: dict[str, Any] | None,
    today: str,
    limit: int = 4,
) -> list[dict[str, str]]:
    """
    Предложения за първото съобщение — какво човекът може да поиска СЕГА. Всяко е текст,
    който влиза в полето за писане; нищо не тръгва само.
    """
    out: list[dict[str, str]] = []
    used_albums = {p.get("album") for p in posts if p.get("album")}
    if library_scan:
        fresh = [a for a in library_scan.get("albums", []) if a["path"] not in used_albums]
        for album in fresh[:2]:
            when = f" от {album['date']}" if album.get("date") else ""
            out.append({
                "kind": "album",
                "album": album["path"],
                "title": f"Албум „{album['name']}“",
                "text": f"Напиши пост за албума „{album['name']}“{when} ({album['count']} снимки). Ето какво се случи: ",
            })
        if library_scan.get("schedules"):
            sched = library_scan["schedules"][0]
            out.append({
                "kind": "schedule",
                "title": f"График „{sched['name']}“",
                "text": f"Прочети графика „{sched['path']}“ от папката и го превърни в постове по датите.",
            })
    empty = [p for p in posts if p.get("needs_brief") and (p.get("scheduled_for") or "") >= today]
    if empty:
        slot = sorted(empty, key=lambda p: p.get("scheduled_for") or "")[0]
        out.append({
            "kind": "slot",
            "title": f"Празно място на {slot['scheduled_for']}",
            "text": f"Какво да публикуваме на {slot['scheduled_for']}? Идеи по стратегията на клъстера.",
        })
    upcoming = [p for p in posts if (p.get("scheduled_for") or "") >= today]
    if not upcoming:
        out.append({
            "kind": "plan",
            "title": "План за следващия месец",
            "text": "Направи ми план за следващия месец — 2 поста седмично, вторник и четвъртък в 10:00.",
        })
    notes = [p for p in posts if p.get("status") and p.get("status") != "ready" and not p.get("published")]
    if notes:
        out.append({
            "kind": "review",
            "title": f"{len(notes)} поста с бележки",
            "text": "Кои постове чакат преглед и какво трябва да поправя по тях?",
        })
    if len(out) < limit:
        out.append({
            "kind": "week",
            "title": "Тази седмица",
            "text": "Какво е планирано за тази седмица и какво още липсва?",
        })
    return out[:limit]


def relative_time_warning(text: str, scheduled_for: str | None, written_on: str | None) -> str | None:
    """
    Пост, написан на една дата и насрочен за друга, който казва „утре"/„вчера"/„тази седмица" —
    думата сочи деня на писане, а човекът ще я прочете в деня на публикуване. Само бележка;
    текстът не се пипа (както _flag_weekday_mismatch в formatting.py).
    """
    if not text or not scheduled_for or not written_on or scheduled_for == written_on:
        return None
    found: list[str] = []
    for match in _RELATIVE_TIME.finditer(text):
        word = match.group(1).lower()
        if word not in found:
            found.append(word)
    if not found:
        return None
    words = ", ".join(f"„{w}“" for w in found[:3])
    return (f"Текстът казва {words}, а е написан на {written_on} за публикуване на {scheduled_for} — "
            "в деня на публикуване може да не е вярно. Поправете думата или датата.")
