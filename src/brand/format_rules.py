"""
Чете МЕХАНИЧНИТЕ правила за формат от корпуса и ги излага като данни, които кодът може
да налага без да пита модел.

Защо съществува този модул. На 2026-08-11 Критикът флагна емоджитата 📍 и 📅 като
неразрешени и изброи „разрешения списък (🙏, 🤝, 📖, 🔗, ✅, 🔹, 👏, ✨)". Проверката беше
вярна по същество — списъкът наистина е в knowledge/voice/tone.md — но ✨ го НЯМА там.
Моделът цитираше правилото по памет. Правило, което се брои и сравнява (кои емоджита,
колко, има ли хаштаг, колко думи, кой линк), не бива да зависи от паметта на модел:
или се налага детерминирано, или не съществува.

Правилата НЕ се хардкодват тук. Четат се от knowledge/, защото корпусът е на клъстера —
ако маркетинг екипът реши, че 📍 е разрешено, той редактира tone.md, не този файл.
Когато ред от корпуса не може да се разчете, съответното правило просто не се налага
(и това се вижда в `unparsed`) — по-добре липсващо правило, отколкото измислено.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Диапазони, покриващи емоджитата, които реално се срещат в постовете (символи, стрелки,
# пиктограми). Нарочно НЕ покрива 0x2000–0x206F — там са тиретата и кавичките („–", „…"),
# които са обикновена типография, не емоджи.
_EMOJI_RE = re.compile(
    "["
    "\U00002190-\U000021FF"  # стрелки
    "\U00002300-\U000023FF"  # технически символи (⌚, ⏰)
    "\U000025A0-\U000027BF"  # геометрични, ✅, ✨, ✔
    "\U00002B00-\U00002BFF"  # още геометрични
    "\U0001F000-\U0001FAFF"  # основният блок емоджи
    "]"
)
_HASHTAG_RE = re.compile(r"(?<!\w)#[^\s#]+")
_RANGE_RE = re.compile(r"(\d+)\s*[–—-]\s*(\d+)")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|(?<![\w@.])(?:www\.)[^\s<>\"')\]]+")

# Месеците се ползват на две места, за да се провери дали един ред носи конкретен факт:
# в pipeline/formatting.py (правилото „отваряне с число или дата" от корпуса) и в
# quality/metrics.py (кои изречения са без нито един факт). Стоят тук, защото това е
# модулът с текстовите примитиви — два отделни списъка биха се разминали при първата
# редакция и двете проверки биха давали различен отговор за един и същи ред.
MONTHS = (
    "януари", "февруари", "март", "април", "май", "юни",
    "юли", "август", "септември", "октомври", "ноември", "декември",
)


@dataclass(frozen=True)
class FormatRules:
    """Механичните правила, изведени от корпуса. `None` значи „правилото не е зададено"."""

    allowed_emoji: frozenset[str] = frozenset()
    emoji_min: int | None = None
    emoji_max: int | None = None
    hashtags_forbidden: bool = False
    opening_needs_fact: bool = False
    words_min: int | None = None
    words_max: int | None = None
    known_urls: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()  # точните редове от корпуса, от които е изведено
    unparsed: tuple[str, ...] = field(default_factory=tuple)  # правила, които не се четат

    @property
    def enforces_emoji(self) -> bool:
        return bool(self.allowed_emoji)

    def known_hosts(self) -> tuple[str, ...]:
        hosts = []
        for url in self.known_urls:
            host = url.split("//")[-1].split("/")[0].lower()
            hosts.append(host.removeprefix("www."))
        return tuple(dict.fromkeys(host for host in hosts if host))


def extract_emoji(text: str) -> list[str]:
    """Всички емоджи в текста, по реда на срещане (с дубликати)."""
    return _EMOJI_RE.findall(text)


def find_hashtags(text: str) -> list[str]:
    """Всички хаштагове в текста, напр. ['#ai', '#варна']."""
    return _HASHTAG_RE.findall(text)


def find_urls(text: str) -> list[str]:
    """Всички http(s):// и www. адреси в текста, с дубликати."""
    return _URL_RE.findall(text)


def count_words(text: str) -> int:
    """Брой думи по разделители — общата дефиниция, ползвана от formatting.py и quality/metrics.py."""
    return len([word for word in re.split(r"\s+", text.strip()) if word])


def _bullets(text: str) -> list[str]:
    """
    Разбива markdown текст на булети, като СЛЕПВА продълженията на следващ ред.

    Наложително, не козметика: емоджи правилото в tone.md се пренася на два реда и
    ред-по-ред четене изпускаше 👏 от разрешения списък (открито веднага при първата
    проверка срещу реалния корпус, 2026-08-11). Правило, прочетено наполовина, е по-лошо
    от непрочетено — то тихо забранява нещо позволено.
    """
    bullets: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        is_new_bullet = stripped.startswith(("- ", "* ", "+ "))
        is_continuation = bool(current) and raw_line.startswith((" ", "\t")) and stripped
        if is_new_bullet:
            if current:
                bullets.append(" ".join(current))
            current = [stripped]
        elif is_continuation:
            current.append(stripped)
        else:
            if current:
                bullets.append(" ".join(current))
                current = []
            if stripped:
                bullets.append(stripped)
    if current:
        bullets.append(" ".join(current))
    return bullets


def _line_containing(text: str, *needles: str) -> str | None:
    for bullet in _bullets(text):
        lowered = bullet.lower()
        if all(needle.lower() in lowered for needle in needles):
            return bullet
    return None


def parse_format_rules(
    tone_text: str = "",
    formats_text: str = "",
    facts: dict[str, Any] | None = None,
) -> FormatRules:
    """
    Изважда правилата от текста на voice/tone.md и voice/formats.md + познатите URL от
    identity/facts.yaml. Всяко правило се търси по ключова дума в отделен ред — нарочно
    просто, за да остане предвидимо кой ред от корпуса поражда кое правило.
    """
    sources: list[str] = []
    unparsed: list[str] = []
    combined = f"{tone_text}\n{formats_text}"

    # --- емоджи: кои са разрешени и колко на пост
    allowed: frozenset[str] = frozenset()
    emoji_min = emoji_max = None
    emoji_line = _line_containing(combined, "emoji") or _line_containing(combined, "емоджи")
    if emoji_line:
        found = {char for char in extract_emoji(emoji_line)}
        if found:
            allowed = frozenset(found)
            sources.append(emoji_line)
            range_match = _RANGE_RE.search(emoji_line)
            if range_match:
                emoji_min, emoji_max = int(range_match.group(1)), int(range_match.group(2))
        else:
            unparsed.append("разрешени емоджи (редът е намерен, но не съдържа емоджи символи)")
    else:
        unparsed.append("разрешени емоджи (няма ред за емоджи в tone.md/formats.md)")

    # Маркерите за списък с акценти (formats.md) са също разрешени емоджи — иначе кодът би
    # изтрил точно онова, което формате изисква.
    bullet_line = _line_containing(formats_text, "списък") or _line_containing(combined, "акценти")
    if bullet_line:
        bullet_emoji = set(extract_emoji(bullet_line))
        if bullet_emoji:
            allowed = frozenset(allowed | bullet_emoji)
            sources.append(bullet_line)

    # --- хаштагове
    hashtag_line = _line_containing(combined, "хаштаг")
    hashtags_forbidden = False
    if hashtag_line:
        lowered = hashtag_line.lower()
        hashtags_forbidden = any(word in lowered for word in ("не ", "без", "никога"))
        sources.append(hashtag_line)
    else:
        unparsed.append("хаштагове (няма ред за хаштагове в корпуса)")

    # --- отваряне с конкретен факт (число/дата/място)
    opening_line = _line_containing(combined, "отваряне")
    opening_needs_fact = False
    if opening_line:
        opening_needs_fact = any(word in opening_line.lower() for word in ("факт", "число", "дата"))
        sources.append(opening_line)
    else:
        unparsed.append("отваряне с конкретен факт (няма ред за отваряне в корпуса)")

    # --- дължина в думи
    words_min = words_max = None
    length_line = _line_containing(combined, "дължина")
    if length_line:
        range_match = _RANGE_RE.search(length_line)
        if range_match:
            words_min, words_max = int(range_match.group(1)), int(range_match.group(2))
            sources.append(length_line)
        else:
            unparsed.append("дължина (редът е намерен, но без числов диапазон)")
    else:
        unparsed.append("дължина (няма ред за дължина в корпуса)")

    # --- познати URL: единствените, които постът има право да съдържа
    known: list[str] = []
    if facts:
        organization = facts.get("organization") or {}
        channels = facts.get("channels") or {}
        for value in (organization.get("website"), channels.get("linkedin_url")):
            if isinstance(value, str) and value.strip():
                known.append(value.strip())
    if not known:
        unparsed.append("познати URL (facts.yaml няма website/linkedin_url)")

    return FormatRules(
        allowed_emoji=allowed,
        emoji_min=emoji_min,
        emoji_max=emoji_max,
        hashtags_forbidden=hashtags_forbidden,
        opening_needs_fact=opening_needs_fact,
        words_min=words_min,
        words_max=words_max,
        known_urls=tuple(known),
        sources=tuple(sources),
        unparsed=tuple(unparsed),
    )


def load_format_rules(knowledge_dir: Path, facts: dict[str, Any] | None = None) -> FormatRules:
    """Чете tone.md/formats.md направо от диска и връща FormatRules — удобство за тестове/скриптове;
    load_brand_knowledge() в brand/loader.py прави същото като част от пълното зареждане на корпуса."""

    def read(rel_path: str) -> str:
        path = knowledge_dir / rel_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return parse_format_rules(read("voice/tone.md"), read("voice/formats.md"), facts)
