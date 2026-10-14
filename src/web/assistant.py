"""
Асистентът — разговор САМО за LinkedIn страницата на клъстера (docs/technical/web-ui-3.md).

Човекът пише свободно („план за ноември, 2 поста седмично", „какво е насрочено тази
седмица", „пост за албума със стажантите"), а асистентът отговаря кратко и, когато е
поискано, предлага ПЛАН — дати, часове, теми, бриф и албум от папката. Планът е само
предложение: нищо не се създава, докато човекът не натисне „Създай" (server.apply_plan).

Правила в код, не в модела (както навсякъде в проекта):
  - извън темата (on_topic=false) → фиксиран учтив отказ, планът се изхвърля;
  - дата/час/режим/албум се проверяват тук; невалидното отпада или става подразбиране;
  - бриф без съдържание → needs_brief: мястото стои в календара, но не се пише текст,
    докато човекът не каже какво се е случило (асистентът не измисля събития).

Разговорите живеят в posts/_assistant/<id>.json — едно съобщение на ред, само добавяне.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.pipeline import modes
from src.pipeline.util import read_json, read_text_or_none, write_json
from src.web import schedule

ASSISTANT_DIR = "_assistant"
MAX_PLAN_ITEMS = 40
HISTORY_TURNS = 12
DEFAULT_TIME = "10:00"
_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
_WEEKDAYS = ("понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя")

OFF_TOPIC_REPLY = (
    "Тук помагам само с LinkedIn страницата на ИКТ Клъстер – Варна: постове, график за "
    "публикуване, теми, снимки от папката. Попитайте ме нещо от това — напр. „направи план "
    "за ноември“ или „какво е насрочено тази седмица“."
)


class AssistantError(Exception):
    """Съобщението е за човека."""


# --------------------------------------------------------------------------- схема


class PlanItem(BaseModel):
    date: str = Field(description="дата на публикуване, YYYY-MM-DD")
    time: str = Field(default=DEFAULT_TIME, description="час, HH:MM")
    title: str = Field(description="кратко заглавие/тема, до 60 знака")
    brief: str = Field(
        default="",
        description=(
            "какво се е случило — САМО с факти, казани от човека в разговора или видими от "
            "името на албума; празно, ако човекът още не е казал"
        ),
    )
    mode: str = Field(default=modes.DEFAULT_MODE, description="kadar | fokus | panorama")
    idea: str = Field(default="", description="твое предложение какво да е съдържанието (тема/ъгъл), когато човекът не е дал факти")
    album: str | None = Field(default=None, description="път на албум от папката (точно както е в списъка) или null")
    needs_brief: bool = Field(default=False, description="true, ако човекът още не е дал съдържание за този пост")


class AssistantTurn(BaseModel):
    on_topic: bool = Field(description="false, ако съобщението не е за LinkedIn присъствието на клъстера")
    reply: str = Field(description="отговор към човека, на български, до ~120 думи, без markdown таблици")
    plan: list[PlanItem] = Field(default_factory=list, description="само ако човекът иска пост/график; иначе []")
    write_now: bool = Field(
        default=False,
        description="true, ако човекът описва ЕДНО конкретно събитие/новина и иска пост за него — пиши го веднага",
    )


SYSTEM = """Ти си асистентът на LinkedIn страницата на ИКТ Клъстер – Варна (сдружение от ~40 фирми,
университети, НПО и гимназии). Работиш с маркетинг екипа на клъстера.

ТЕМА: говориш САМО за LinkedIn присъствието на клъстера — какво и кога да се публикува,
график/календар, теми и рубрики, снимки от папката, преглед на насрочените и готовите постове,
добри практики за LinkedIn. Всичко друго (общи въпроси, код, лични теми, други мрежи, новини)
→ on_topic=false и кратък reply. Поздрав или „благодаря“ е on_topic=true.

ФАКТИ: никога не измисляй събития, числа, имена, цитати или резултати. brief пишеш само с
това, което човекът е казал в разговора, или което личи от името на албум в папката. Албум с
„ТЕКСТ ОТ ПАПКАТА“ е готов пост от човека: сложи album и копирай текста ДОСЛОВНО в brief. Твоите
предложения за съдържание („представяне на член“, „обзор на месеца“) отиват в idea, НЕ в
brief. Ако човекът иска N поста, но не е казал за какво — title и idea, brief празен,
needs_brief=true. Не обещавай, че нещо е публикувано.

ПЛАН: попълвай plan само когато човекът иска пост или график. Когато го иска — ВЪРНИ ПЛАНА
ВЕДНАГА, без уточняващи въпроси преди него: липсващото попълни с подразбиране (дни вторник и
четвъртък, час 10:00, теми — рубрики за клъстер), а в reply кажи с едно изречение какво си
приел и че всичко се поправя в таблицата. Конкретно събитие, което човекът е споменал (дата,
час, какво), влиза като отделен ред с brief от думите му. Дати — само бъдещи (от днес
нататък), YYYY-MM-DD. Час по подразбиране 10:00, освен ако човекът не каже друго. Режими:
kadar (40–90 думи, бързо съобщение/покана), fokus (стандартен), panorama (400–650 думи,
история с много факти). album — само път от списъка АЛБУМИ по-долу, точно изписан.
Ако планът засяга ден, в който вече има пост, кажи го в reply. „N поста седмично за месец"
значи N за ВСЯКА седмица от месеца — от първата до последната, не само от днес нататък.
Покана или анонс излиза ПРЕДИ крайния срок/събитието, никога след него.

ЕДИН ПОСТ СЕГА: ако човекът разказва какво се е случило (или ще се случи) и иска пост за това —
или просто описва събитие — write_now=true и plan с ТОЧНО един ред: date = днес (или денят, за
който човекът иска да излезе), brief = думите му ДОСЛОВНО (плюс текста от папката, ако посочи
албум), album — ако споменава албум/папка. reply: едно изречение, че пишеш поста. Не питай нищо.

ОТГОВОР: кратък, конкретен, на български, на „Вие“. Без markdown таблици — планът се показва
отделно като таблица. Върни САМО JSON по схемата."""


# --------------------------------------------------------------------------- съхранение


def _dir(posts_dir: Path) -> Path:
    return posts_dir / ASSISTANT_DIR


def thread_path(posts_dir: Path, thread_id: str) -> Path:
    if not _ID_RE.match(thread_id or ""):
        raise AssistantError("Няма такъв разговор.")
    return _dir(posts_dir) / f"{thread_id}.json"


def create_thread(posts_dir: Path) -> dict[str, Any]:
    thread = {"id": uuid.uuid4().hex[:12], "title": "Нов разговор", "created": round(time.time()), "messages": []}
    save_thread(posts_dir, thread)
    return thread


def load_thread(posts_dir: Path, thread_id: str) -> dict[str, Any]:
    path = thread_path(posts_dir, thread_id)
    try:
        return read_json(path)
    except (OSError, ValueError) as exc:
        raise AssistantError("Няма такъв разговор.") from exc


def save_thread(posts_dir: Path, thread: dict[str, Any]) -> None:
    folder = _dir(posts_dir)
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / f"{thread['id']}.json", thread)


def list_threads(posts_dir: Path) -> list[dict[str, Any]]:
    folder = _dir(posts_dir)
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob("*.json"):
        try:
            thread = read_json(path)
        except (OSError, ValueError):
            continue
        messages = thread.get("messages") or []
        items.append({
            "id": thread.get("id"),
            "title": thread.get("title") or "Разговор",
            "modified": path.stat().st_mtime,
            "count": len(messages),
        })
    items.sort(key=lambda t: t["modified"], reverse=True)
    return items[:100]


def delete_thread(posts_dir: Path, thread_id: str) -> None:
    path = thread_path(posts_dir, thread_id)
    if path.exists():
        trash = posts_dir / "_kosh" / ASSISTANT_DIR
        trash.mkdir(parents=True, exist_ok=True)
        path.replace(trash / path.name)


# --------------------------------------------------------------------------- контекст


def _context(today: date, calendar: list[dict[str, Any]], library_scan: dict[str, Any] | None) -> str:
    lines = [f"ДНЕС: {today.isoformat()} ({_WEEKDAYS[today.weekday()]})."]
    horizon = (today + timedelta(days=120)).isoformat()
    upcoming = sorted(
        (p for p in calendar if p.get("scheduled_for") and today.isoformat() <= p["scheduled_for"] <= horizon),
        key=lambda p: (p["scheduled_for"], p.get("scheduled_time") or ""),
    )
    if upcoming:
        lines.append("\nНАСРОЧЕНИ ПОСТОВЕ:")
        for p in upcoming[:60]:
            state = ("публикуван" if p.get("published") else "одобрен" if p.get("approved")
                     else "чака бриф" if p.get("needs_brief") else "готов текст" if p.get("status") else "чернова")
            lines.append(f"- {p['scheduled_for']} {p.get('scheduled_time') or ''} · {p.get('title')} · {state}")
    else:
        lines.append("\nНАСРОЧЕНИ ПОСТОВЕ: няма.")
    recent = [p for p in calendar if p.get("published")][:8]
    if recent:
        lines.append("\nПОСЛЕДНО ПУБЛИКУВАНИ: " + "; ".join(str(p.get("title")) for p in recent))
    if library_scan:
        lines.append(f"\nПАПКА „{library_scan.get('name')}“ — АЛБУМИ (път · снимки · дата от името):")
        for album in library_scan.get("albums", [])[:60]:
            lines.append(f"- {album['path'] or '.'} · {album['count']} · {album.get('date') or '—'}")
            if album.get("brief"):
                # Папка = пост: текстът до снимките е брифът, написан от човека.
                excerpt = " ".join(album["brief"].split())[:600]
                lines.append(f"  ТЕКСТ ОТ ПАПКАТА (факти от човека): {excerpt}")
        if library_scan.get("schedules"):
            lines.append("ФАЙЛОВЕ С ГРАФИК: " + ", ".join(s["path"] for s in library_scan["schedules"][:10]))
    else:
        lines.append("\nПАПКА: не е избрана (човекът може да я избере от „Папка“).")
    return "\n".join(lines)


def _prompt(thread: dict[str, Any], text: str, context: str) -> str:
    history = []
    for m in (thread.get("messages") or [])[-HISTORY_TURNS:]:
        who = "ЧОВЕК" if m.get("role") == "user" else "АСИСТЕНТ"
        body = m.get("text") or ""
        if m.get("plan"):
            body += "\n[предложен план: " + "; ".join(
                f"{i.get('date')} {i.get('title')}" for i in m["plan"][:20]) + "]"
        history.append(f"{who}: {body}")
    parts = [context]
    if history:
        parts.append("\nДОСЕГАШЕН РАЗГОВОР:\n" + "\n".join(history))
    parts.append(f"\nНОВО СЪОБЩЕНИЕ ОТ ЧОВЕКА:\n{text}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- валидиране


_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁёЪъЬьЮюЯя]{4,}")
_BRIEF_DATE = re.compile(
    r"\b(\d{1,2}\s+(?:януари|февруари|март|април|май|юни|юли|август|септември|октомври|ноември|декември)"
    r"(?:\s+\d{4})?|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)",
    re.IGNORECASE,
)


def dates_in(text: str, today: date) -> list[str]:
    """Датите, споменати в брифа („20 ноември", „15.11") — ISO, подредени."""
    found = set()
    for match in _BRIEF_DATE.finditer(text or ""):
        iso = schedule.parse_date(match.group(1), today=today)
        if iso:
            found.add(iso)
    return sorted(found)


def _stems(text: str) -> set[str]:
    """Думи от 4+ букви, срязани до 5 знака — грубо, но стига за българските окончания."""
    return {w.lower()[:5] for w in _WORD.findall(text or "")}


def grounded(brief: str, source: str, threshold: float = 0.6) -> bool:
    """
    Брифът „идва от човека", ако поне `threshold` от думите му ги има в казаното от човека
    (или в имената на албумите). Иначе е идея на модела — не факт, и не отива към Писателя.
    """
    words = _stems(brief)
    if not words:
        return False
    return len(words & _stems(source)) / len(words) >= threshold


def _norm(text: str) -> str:
    """Сравнение на имена без значение от тирета, кавички, главни букви и интервали."""
    text = re.sub(r"[-‐-―−_\"„“”«»'`]+", " ", (text or "").lower())
    text = re.sub(r"\(\d+\)\s*$", "", text.strip())  # „Пост 1 - Хакатон (3)" — броят снимки от списъка
    return " ".join(text.replace("\\", "/").strip("/ ").split())


def match_album(raw: Any, title: str, albums: dict[str, str | None]) -> str | None:
    """
    Албумът на реда — толерантно, защото моделът изписва имената неточно (живо, 2026-10-14:
    позна трите поста по заглавие, но върна album=null). По ред: точен път → същото име без
    тирета/кавички → името съдържа или се съдържа → заглавието на реда съвпада с името или
    текста на ТОЧНО един албум.
    """
    if raw:
        wanted = _norm(str(raw))
        for path in albums:
            if _norm(path) == wanted or _norm(path.rsplit("/", 1)[-1]) == wanted:
                return path
        partial = [p for p in albums if wanted and (wanted in _norm(p) or _norm(p.rsplit("/", 1)[-1]) in wanted)]
        if len(partial) == 1:
            return partial[0]
    words = _stems(title)
    if len(words) < 2:
        return None
    scored = []
    for path, brief in albums.items():
        overlap = len(words & _stems(f"{path} {brief or ''}")) / len(words)
        if overlap >= 0.5:
            scored.append((overlap, path))
    scored.sort(reverse=True)
    if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.25):
        return scored[0][1]
    return None


def clean_plan(
    items: list[PlanItem] | list[dict[str, Any]],
    today: date,
    albums: set[str] | dict[str, str | None],
    source_text: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Правилата на плана — в код. Връща (чистите редове, бележки за човека).

    `source_text` — казаното от човека в разговора + имената на албумите. Подаден ли е,
    брифове, които не идват оттам, стават „идея" (needs_brief). При „Създай" не се подава:
    тогава брифът е минал през ръцете на човека в таблицата.
    """
    album_briefs: dict[str, str | None] = albums if isinstance(albums, dict) else {a: None for a in albums}
    cleaned: list[dict[str, Any]] = []
    dropped_dates = 0
    ungrounded = 0
    taken: set[str] = set()
    for raw in items[:MAX_PLAN_ITEMS]:
        item = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
        when = schedule.parse_date(item.get("date"), today=today)
        if not when or when < today.isoformat():
            dropped_dates += 1
            continue
        match = _TIME_RE.match(str(item.get("time") or "").strip())
        at = f"{int(match.group(1)):02d}:{match.group(2)}" if match else DEFAULT_TIME
        album = match_album(item.get("album"), str(item.get("title") or ""), {
            p: b for p, b in album_briefs.items() if p not in taken or p == item.get("album")})
        if album:
            taken.add(album)
        brief = " ".join(str(item.get("brief") or "").split())[: schedule.MAX_BRIEF_CHARS]
        from_folder = False
        if album and album_briefs.get(album) and (not brief or (source_text is not None and not grounded(brief, source_text))):
            # Папка = пост: текстът до снимките е написан от човека — той е брифът, дословно,
            # вместо празен бриф или преразказ на модела.
            brief = " ".join(album_briefs[album].split())[: schedule.MAX_BRIEF_CHARS]
            from_folder = True
        idea = " ".join(str(item.get("idea") or "").split())[:400]
        if brief and not from_folder and source_text is not None and not grounded(brief, source_text):
            idea = idea or brief
            brief = ""
            ungrounded += 1
        title = " ".join(str(item.get("title") or "").split())[:80] or (brief[:60] if brief else "Пост")
        cleaned.append({
            "date": when,
            "time": at,
            "title": title,
            "brief": brief,
            "idea": idea,
            "mode": modes.resolve_mode(str(item.get("mode") or "")) or modes.DEFAULT_MODE,
            "album": album,
            "needs_brief": not brief,  # има ли факти, решава брифът, не флагът на модела
        })
    cleaned.sort(key=lambda i: (i["date"], i["time"]))
    notes = []
    for item in cleaned:
        # Пост след датата, за която говори (срок за записване, самото събитие) — почти винаги
        # грешка на плана: поканата излиза, след като е късно. Показва се, не се поправя само.
        past = [d for d in dates_in(item["brief"], today) if d < item["date"]]
        if past:
            item["warning"] = f"Постът е на {item['date']}, а брифът споменава {past[0]} — да не е късно?"
            notes.append(f"„{item['title']}“: {item['warning']}")
    if dropped_dates:
        notes.append(f"{dropped_dates} от предложените дати бяха в миналото или неясни — махнах ги.")
    if ungrounded:
        notes.append(
            f"{ungrounded} от описанията бяха мои идеи, а не факти от вас — оставих ги като идеи. "
            "Агентът пише текст само по това, което вие сте казали."
        )
    return cleaned, notes


# --------------------------------------------------------------------------- разговор


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(thread_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(thread_id, threading.Lock())


def respond(
    posts_dir: Path,
    thread_id: str,
    text: str,
    *,
    client: Any,
    calendar: list[dict[str, Any]],
    library_scan: dict[str, Any] | None,
    today: date | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Едно ходене: съобщението на човека → отговор (и евентуално план). Записва и двете."""
    from src.llm.roles import Role  # тук, за да не тегли llm слоя при всеки import на сървъра

    text = (text or "").strip()
    if not text:
        raise AssistantError("Напишете какво искате да планираме.")
    lock = _lock_for(thread_id)
    if not lock.acquire(blocking=False):
        raise AssistantError("Асистентът още отговаря на предишното съобщение — изчакайте.")
    try:
        today = today or datetime.now().date()
        thread = load_thread(posts_dir, thread_id)
        context = _context(today, calendar, library_scan)
        hint = strategy_hint()
        if hint:
            context += f"\n\nСТРАТЕГИЯ НА КЛЪСТЕРА (от knowledge/):\n{hint}"
        started = time.monotonic()
        turn = client.complete(Role.PLANNER, _prompt(thread, text, context), system=SYSTEM, schema=AssistantTurn)
        seconds = round(time.monotonic() - started, 1)

        thread.setdefault("messages", []).append({"role": "user", "text": text[:4000], "t": round(time.time())})
        if not turn.on_topic:
            agent = {"role": "agent", "text": OFF_TOPIC_REPLY, "off_topic": True}
        else:
            write_now = bool(turn.write_now) and len(turn.plan) == 1
            if write_now:
                # Един пост „сега": без дата → днес; режимът е избраният от човека в лентата.
                item = turn.plan[0]
                if not schedule.parse_date(item.date, today=today) or schedule.parse_date(item.date, today=today) < today.isoformat():
                    item.date = today.isoformat()
                if modes.resolve_mode(mode or ""):
                    item.mode = modes.resolve_mode(mode)
            album_list = (library_scan or {}).get("albums", [])
            albums = {a["path"]: a.get("brief") for a in album_list}
            said = [m.get("text") or "" for m in thread["messages"] if m.get("role") == "user"]
            # Текстът в папката е написан от човека — факт, както казаното в разговора.
            source = "\n".join(said + sorted(albums) + [a["brief"] for a in album_list if a.get("brief")])
            plan, notes = clean_plan(turn.plan, today, albums, source_text=source)
            reply = (turn.reply or "").strip() or ("Ето предложение." if plan else "Добре.")
            agent = {"role": "agent", "text": reply[:3000]}
            if plan:
                agent["plan"] = plan
                if write_now and plan[0]["brief"]:
                    agent["write_now"] = True
            if notes:
                agent["notes"] = notes
        agent.update({"t": round(time.time()), "seconds": seconds})
        thread["messages"].append(agent)
        if thread.get("title") in (None, "", "Нов разговор") and not agent.get("off_topic"):
            thread["title"] = " ".join(text.split())[:60]
        save_thread(posts_dir, thread)
        return thread
    finally:
        lock.release()


def mark_applied(posts_dir: Path, thread_id: str, index: int, batch_id: str, count: int) -> dict[str, Any]:
    thread = load_thread(posts_dir, thread_id)
    messages = thread.setdefault("messages", [])
    if 0 <= index < len(messages):
        messages[index]["applied"] = batch_id
    messages.append({
        "role": "agent", "kind": "note", "t": round(time.time()),
        "text": f"Създадох {count} {'пост' if count == 1 else 'поста'} в календара.",
        "batch": batch_id,
    })
    save_thread(posts_dir, thread)
    return thread


def strategy_hint(knowledge_dir: Path = Path("knowledge")) -> str:
    """Кратък откъс от стратегията за контекст — празно, докато файловете са само TODO."""
    chunks = []
    for name in ("strategy/pillars.md", "strategy/goals.md", "identity/about.md"):
        text = read_text_or_none(knowledge_dir / name) or ""
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL).strip()
        if text:
            chunks.append(text[:800])
    return "\n\n".join(chunks)
