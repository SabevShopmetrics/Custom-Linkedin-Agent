"""
Стъпка 6б — налага механичните правила върху готовото предложение.

Разделението, което този модул въвежда:

  механично (брои се и се сравнява) → КОД, тук, автоматично                → изходът е чист
  преценка (по-добър ли е този израз) → МОДЕЛ (Писател, Критик)            → бележка за човек
  извън текста (честота, час, формат) → ЧОВЕК (виж research-plan-10)       → не влиза в кода

Правилата идват от корпуса през src/brand/format_rules.py — тук няма нито един хардкоднат
емоджи или число. Ако корпусът не задава правило, то не се налага.

Защо се ПОПРАВЯ, а не само се докладва: инструментът трябва винаги да дава публикуем текст
(потребителска инструкция 2026-08-11). Хаштаг, който гласът никога не ползва, или измислен
домейн, не са материя за човешка преценка — те просто не бива да излизат. Нещата, които
кодът не може да реши сам (постът е с 40 думи под минимума; отваря без факт), остават като
бележки, защото поправката им е писане, не чистене.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from src.brand.format_rules import (
    MONTHS,
    FormatRules,
    count_words,
    extract_emoji,
    find_hashtags,
    find_urls,
)

LINK_PLACEHOLDER = "[линк]"

_DIGIT_RE = re.compile(r"\d")

# Хваща изречения, в които моделът "чупи четвъртата стена" — обяснява на читателя ЗАЩО нещо
# липсва (GDPR, поверителност), вместо просто да пропусне детайла мълчаливо. Реален случай
# (2026-08-25): постът съдържаше буквално "не мога да публикувам имената поради GDPR
# съображения" — вярна причина, грешно място. Виж _strip_meta_disclaimers().
_META_DISCLAIMER_RE = re.compile(
    r"(?i)\bGDPR\b"
    r"|поради\s+(съображения\s+за\s+)?поверителност"
    r"|не\s+(мога|можем|бива)\s+да\s+(публикувам|публикуваме|спомена\w*|назова\w*|посоча\w*)"
    r"\s+(имена\w*|снимк\w*|лиц\w*)"
)


@dataclass
class FormattingResult:
    """Резултатът от enforce() — поправеният текст плюс одитна следа какво е пипнато и защо."""

    text: str
    fixes: list[str] = field(default_factory=list)  # какво кодът е поправил сам
    notes: list[str] = field(default_factory=list)  # какво остава за човека/следващ пост
    # Правила, които корпусът не задава достатъчно ясно, за да се наложат. НЕ са бележка
    # към конкретния пост — те са еднакви за всеки пост и мястото им е в run.log и в
    # `cli check-knowledge`, не в списъка, който човек чете преди публикуване.
    unenforceable: list[str] = field(default_factory=list)


def _tidy(text: str) -> str:
    """Маха следи от изтрито: двойни интервали, интервал преди пунктуация, тройни редове."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.!?;:])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_hashtags(text: str, fixes: list[str]) -> str:
    """Маха всеки хаштаг и добавя запис в `fixes` — гласът на клъстера не ги ползва (tone.md)."""
    hashtags = find_hashtags(text)
    if not hashtags:
        return text
    for tag in hashtags:
        text = text.replace(tag, "")
    fixes.append(f"Премахнати хаштагове ({', '.join(hashtags)}) — гласът на клъстера не ги ползва.")
    return text


def _strip_disallowed_emoji(text: str, rules: FormatRules, fixes: list[str]) -> str:
    """Маха всяко емоджи извън rules.allowed_emoji — независимо от бройката, само от разрешеността."""
    disallowed = [char for char in extract_emoji(text) if char not in rules.allowed_emoji]
    if not disallowed:
        return text
    unique = list(dict.fromkeys(disallowed))
    for char in unique:
        text = text.replace(char, "")
    fixes.append(
        f"Премахнати неразрешени емоджи ({' '.join(unique)}) — разрешените в "
        f"knowledge/voice/ са {' '.join(sorted(rules.allowed_emoji))}."
    )
    return text


def _trim_excess_emoji(text: str, rules: FormatRules, fixes: list[str]) -> str:
    """
    Реже само НАДБРОЙНИТЕ, отзад напред — последните емоджи в поста са декоративни
    (в затварящия ред), докато първите носят структура (маркери на списък с акценти).
    """
    if rules.emoji_max is None:
        return text
    found = extract_emoji(text)
    excess = len(found) - rules.emoji_max
    if excess <= 0:
        return text

    removed: list[str] = []
    for char in reversed(found):
        if excess == 0:
            break
        index = text.rfind(char)
        if index != -1:
            text = text[:index] + text[index + len(char) :]
            removed.append(char)
            excess -= 1
    fixes.append(
        f"Премахнати {len(removed)} надбройни емоджи ({' '.join(removed)}) — "
        f"корпусът допуска най-много {rules.emoji_max} на пост."
    )
    return text


def _replace_unknown_urls(text: str, rules: FormatRules, brief_text: str, fixes: list[str]) -> str:
    """
    Всеки URL, който не е в брифа и не е познат от facts.yaml, се заменя с [линк].

    Реален случай (2026-08-11): предложението завърши с `https://ictcluster-varna.org` —
    домейн, който не съществува. Истинският (`ictclustervarna.com`) беше в facts.yaml, в
    същия промпт. Критикът не го хвана, защото проверява твърдения, не низове. Модел не
    може да „познае" URL — или е подаден, или е измислен, и второто не бива да излиза.
    """
    known_hosts = rules.known_hosts()
    replaced: list[str] = []

    for url in dict.fromkeys(find_urls(text)):
        cleaned = url.rstrip(".,;:!?)")
        if cleaned in brief_text:
            continue
        host = cleaned.split("//")[-1].split("/")[0].lower().removeprefix("www.")
        if host in known_hosts:
            continue
        text = text.replace(cleaned, LINK_PLACEHOLDER)
        replaced.append(cleaned)

    if replaced:
        fixes.append(
            f"Заменени непознати адреси с {LINK_PLACEHOLDER} ({', '.join(replaced)}) — не идват от брифа "
            f"и не са в facts.yaml, тоест са измислени от модела. Сложете истинския линк ръчно."
        )
    return text


def _add_missing_thematic_emoji(text: str, rules: FormatRules, fixes: list[str]) -> str:
    """
    Ако постът е под emoji_min след цялата верига (Писател/Критик/Консолидатор), добавя
    тематично емоджи на редове, които вече са структурни — благодарност, затварящ линк —
    вместо само да го отбелязва като бележка за човек.

    Съзнателно НЕ форсира точния минимум на всяка цена: emoji вкарано в наративен абзац
    само за да се вдигне бройката би било точно декорацията, срещу която предупреждава
    новото проучване в knowledge/voice/tone.md (2026-08-17) — "ако премахването му не
    променя структурата, махни го" важи и в обратна посока: не добавяй, ако няма
    структура, към която да се закачи. Ако безопасните места (благодарност, линк) се
    изчерпят преди минимума, остатъкът остава като бележка по-долу в enforce(), не се
    форсира с декоративна вложка.

    Мястото за благодарност се търси на ниво ИЗРЕЧЕНИЕ, не ред — реален пропуск (живо
    тестване, 2026-08-28): "Благодарим на всички 18 дами..." беше второто изречение в общ
    абзац с наратив преди него, а старата проверка гледаше само дали РЕДЪТ започва с
    "благодар". Постът излезе с 0 емоджита без дори опит за поправка. Същият клас проблем
    вече е решен в _strip_placeholder_closing/_strip_meta_disclaimers по-долу, по същата
    причина — тук просто не беше приложено.
    """
    if not rules.enforces_emoji or rules.emoji_min is None:
        return text

    current = len(extract_emoji(text))
    if current >= rules.emoji_min:
        return text

    lines = text.split("\n")
    added: list[str] = []
    nonempty_indices = [i for i, line in enumerate(lines) if line.strip()]
    last_nonempty = nonempty_indices[-1] if nonempty_indices else None

    # Място 1: благодарствено изречение — почти сигурно структурен елемент по гласа на
    # клъстера, независимо дали дели ред с предходно наративно изречение.
    if current < rules.emoji_min and "🙏" in rules.allowed_emoji:
        for i, line in enumerate(lines):
            sentences = re.split(r"(?<=[.!?])\s+", line)
            found_sentence = False
            for j, sentence in enumerate(sentences):
                if sentence.strip().lower().startswith("благодар") and not extract_emoji(sentence):
                    sentences[j] = f"🙏 {sentence}"
                    found_sentence = True
                    break
            if found_sentence:
                lines[i] = " ".join(sentences)
                current += 1
                added.append("🙏 на благодарствено изречение")
                break

    # Място 2: последният непразен ред, ако прилича на затваряща препратка (линк или
    # покана да се види повече) — 🔗 ако носи URL, иначе 📖.
    if current < rules.emoji_min and last_nonempty is not None:
        line = lines[last_nonempty]
        stripped = line.strip()
        looks_like_closing = "вижте повече" in stripped.lower() or "http" in stripped
        if looks_like_closing and not extract_emoji(line):
            emoji = "🔗" if "http" in stripped else "📖"
            if emoji in rules.allowed_emoji:
                lines[last_nonempty] = f"{emoji} {line}"
                current += 1
                added.append(f"{emoji} на затварящия ред")

    if added:
        fixes.append(
            f"Добавени {len(added)} тематични емоджита на вече структурни редове "
            f"({', '.join(added)}) — постът беше под минимума {rules.emoji_min}."
        )
    return "\n".join(lines)


def _strip_placeholder_closing(text: str, fixes: list[str]) -> str:
    """
    Маха изречението, съдържащо [линк] (обикновено затварящата формула "Вижте повече ...
    тук: [линк]"), вместо да го остави видимо в готовия текст.

    Потребителска обратна връзка (2026-08-17): видим placeholder в готов пост изглежда
    "неловко" и "неправилно" — все едно постът е забравен недовършен, дори написаният до
    него текст да е нормален. Писателят вече е инструктиран да не пише такова изречение,
    ако няма реален адрес (виж writer.py) — това тук е код-ниво подсигуряване за случаите,
    в които модел все пак го напише, или в които `_replace_unknown_urls` замести измислен
    URL point и остави placeholder-а сам.

    Работи на ниво изречение, не ред — затварящата формула често дели ред с предходно
    легитимно съдържание ("Благодарим на... Вижте повече тук: [линк]"), и цял ред би
    изтрил и легитимната част.
    """
    if LINK_PLACEHOLDER not in text:
        return text

    removed = False
    new_lines: list[str] = []
    for line in text.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in sentences if LINK_PLACEHOLDER not in s]
        if len(kept) != len(sentences):
            removed = True
        new_lines.append(" ".join(kept).strip())

    if removed:
        fixes.append(
            f"Премахнато изречение с {LINK_PLACEHOLDER} — видим placeholder изглежда като "
            f"недовършен пост; по-добре без затваряща фраза, отколкото с нея без реален линк."
        )
    return "\n".join(new_lines)


_PHOTO_CREDIT_RE = re.compile(r"(?im)^[ \t]*снимк[аи]?\s*:.*$\n?")


def _strip_fake_photo_credit(text: str, has_photos: bool, fixes: list[str]) -> str:
    """
    Маха ред "Снимки: <кредит>", ако Media Picker не е избрал нито една снимка за поста.

    Реален случай (2026-08-28, живо тестване): текстов пост (без снимки в photos/) излезе
    със "Снимки: Архив ИКТ Клъстер – Варна" накрая — Писателят имитира буквално затварящия
    ред от knowledge/examples/good/04-sreshta-pod-zvezdite.md (реален пост, който Е имал
    снимки и Е кредитирал истински фотограф), но е измислил кредита за пост без нито една
    снимка. Нито Критик, нито другите проверки тук хващат това, защото гледат твърдения/
    линкове/емоджита, не съответствие текст↔действително избрани снимки.
    """
    if has_photos or not _PHOTO_CREDIT_RE.search(text):
        return text
    removed = [line.strip() for line in _PHOTO_CREDIT_RE.findall(text)]
    text = _PHOTO_CREDIT_RE.sub("", text)
    fixes.append(
        f"Премахнат измислен ред за снимков кредит ({'; '.join(removed)}) — постът няма нито "
        f"една избрана снимка (Media Picker не избра нищо), значи няма какво да се кредитира."
    )
    return text


def _strip_meta_disclaimers(text: str, notes: list[str]) -> str:
    """
    Маха изречения, в които моделът говори за собствения си процес на писане/цензуриране
    (GDPR, "не мога да публикувам имената поради...") ВЪТРЕ в текста на поста. Причината е
    вярна и важна — но мястото ѝ е бележка към човека (open_notes → ⚠ предупреждение в
    CLI/REPL/TUI), не публикуван текст: читател на LinkedIn пост не бива да вижда вътрешната
    кухня на инструмента, който го е написал. Работи на ниво изречение, не ред — same модел
    като _strip_placeholder_closing() по-долу, за същата причина (изречението често дели ред
    с легитимно съдържание).

    Потребителска инструкция (2026-08-25): "това са глупости и не трябва да са в пост...
    нека да излиза като warning — там е супер и е важно, но НИкога в пост."
    """
    if not _META_DISCLAIMER_RE.search(text):
        return text

    removed: list[str] = []
    new_lines: list[str] = []
    for line in text.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = []
        for sentence in sentences:
            if _META_DISCLAIMER_RE.search(sentence):
                removed.append(sentence.strip())
            else:
                kept.append(sentence)
        new_lines.append(" ".join(kept).strip())

    if removed:
        quoted = " · ".join(f'"{s}"' for s in removed)
        notes.append(
            f"Постът обясняваше в самия текст защо нещо липсва (GDPR/поверителност) — "
            f"премахнато от текста, защото читателят не бива да вижда вътрешната кухня на "
            f"писането: {quoted}. Прегледайте дали липсващото име/детайл наистина трябва да "
            f"остане пропуснато."
        )
    return "\n".join(new_lines)


_LEVA_RE = re.compile(r"\bлв\.?\b|\bлева\b", re.IGNORECASE)
_LEVA_AMOUNT_RE = re.compile(r"[\d][\d\s.,]*\s*(?:лв\.?|лева)", re.IGNORECASE)

# Максимален брой дни по месец — консервативно за февруари (29, без изчисление на високосна
# година; целта е да хване очевидно невъзможни дати като "30 февруари", не да съди гранични
# случаи). Виж _flag_impossible_dates() защо съществува тази проверка.
_MAX_DAYS_BY_MONTH = {
    "януари": 31, "февруари": 29, "март": 31, "април": 30, "май": 31, "юни": 30,
    "юли": 31, "август": 31, "септември": 30, "октомври": 31, "ноември": 30, "декември": 31,
}
_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.IGNORECASE)


def _flag_impossible_dates(text: str, notes: list[str]) -> None:
    """
    Флагва календарно невъзможна дата (напр. "30 февруари") — не я пипа, само предупреждава,
    същия модел като _flag_leva_currency.

    Реален пропуск, хванат при живо тестване (2026-08-26): бриф с невъзможна дата и невярна
    годишнина ("30 февруари... 200-годишнина" за организация, основана 2008 г.). Планерът
    правилно разпозна грешката в бележката си, но Писателят я написа въпреки това — и
    Критикът никога не я хвана, защото проверката му за факти е "проследимо ли е до брифа",
    не "вярно ли е като календар/аритметика", а датата формално ИДВАШЕ от брифа (потребителят
    я е написал грешно). "Готовият" пост излезе с невъзможна дата, без нито едно предупреждение
    към човека. Тази проверка е механичен, надежден бекъп — не заменя виж critic.py
    (_render_planner_notes_block), който сега подава бележката на Планера директно на Критика
    за по-общия клас "брифът съдържа факт, който противоречи сам на себе си".
    """
    for match in _DATE_RE.finditer(text):
        day = int(match.group(1))
        month = match.group(2).lower()
        max_day = _MAX_DAYS_BY_MONTH.get(month)
        if max_day is not None and day > max_day:
            notes.append(
                f"Датата \"{match.group(0)}\" е календарно невъзможна ({month} има най-много "
                f"{max_day} дни) — проверете и коригирайте преди публикуване."
            )


def _flag_leva_currency(text: str, notes: list[str]) -> None:
    """
    Флагва спомената сума в лева — България е в еврозоната (потребителска инструкция
    2026-08-25: "България е самоо в евро"), затова "30 лв." в НОВ пост почти сигурно
    трябва да е в евро. Само бележка, НЕ пипа текста: ако е изрично писано в лева (напр.
    исторически факт, като сумата от благотворителния бал, събрана преди приемането на
    еврото — виж identity/facts.yaml, first_ball_raised), то си остава непроменено —
    курсът лев→евро не е нещо, което кодът трябва да гадае вместо човек.
    """
    if not _LEVA_RE.search(text):
        return
    amounts = list(dict.fromkeys(match.strip() for match in _LEVA_AMOUNT_RE.findall(text)))
    shown = ", ".join(f'"{amount}"' for amount in amounts) if amounts else "лв./лева"
    notes.append(
        f"Постът споменава сума в лева ({shown}) — България е в еврозоната. Ако не е "
        f"исторически факт (сума отпреди приемането на еврото), проверете дали не трябва "
        f"да е в евро; ако е нарочно в лева, оставете го непроменено."
    )


_ANNIVERSARY_RE = re.compile(r"(\d{1,3})[ \t-]*годишнина")


def _flag_anniversary_mismatch(text: str, founded_year: int | None, today: date, notes: list[str]) -> None:
    """
    Флагва "N-годишнина", чието N не съвпада с today.year - founded_year от facts.yaml —
    само бележка, не пипа текста (същия модел като _flag_leva_currency).

    Реален случай (живо тестване, 2026-08-31, виж
    research-plan-25-adversarial-live-testing-findings.md, Б-2): бриф "тази година чества
    5-годишнина" (клъстерът е основан 2008 г., 2026 г. = 18-годишнина) премина непроменено
    през Планер → Писател → Критик (два пъти) → Консолидатор — публикуваният текст съдържаше
    буквално и "основан през 2008 г.", и "5-годишнина" в едно изречение. За разлика от
    _flag_impossible_dates по-горе, тук двете числа поотделно са валидни (2008 е истинска
    година, 5 е представимо число за годишнина) — само комбинацията им е аритметично грешна,
    затова изисква изричното сравнение тук, не разпознаване на невъзможен формат.
    """
    if founded_year is None:
        return
    expected = today.year - founded_year
    for match in _ANNIVERSARY_RE.finditer(text):
        claimed = int(match.group(1))
        if claimed != expected:
            notes.append(
                f"Постът споменава \"{claimed}-годишнина\", но според facts.yaml (основан "
                f"{founded_year} г.) {today.year} г. отговаря на {expected}-годишнина. "
                f"Проверете и коригирайте преди публикуване."
            )


# Скриптове, които не би трябвало да се появят в българскоезичен пост — реален случай
# (живо тестване, 2026-08-28): Post I съдържаше "Менторите协助стваха", китайски йероглифи
# залепени вътре в българска дума — generation грешка на модела. Критикът я хвана тогава с
# LLM преценка, но разчитането само на модел да забележи чужда писменост всеки път не е
# надеждно — това е евтин механичен бекъп, същия модел като _flag_impossible_dates по-долу.
# Диапазоните покриват CJK, хирагана/катакана, хангъл, арабица, иврит и тайландска
# писменост — достатъчно широко за реални generation-глитчове, без да засяга кирилица,
# латиница или емоджитата (отделен диапазон, вече обработени другаде).
_FOREIGN_SCRIPT_RE = re.compile(
    "["
    "一-鿿"  # CJK Unified Ideographs
    "぀-ヿ"  # хирагана + катакана
    "가-힣"  # хангъл
    "؀-ۿ"  # арабица
    "֐-׿"  # иврит
    "฀-๿"  # тайландска писменост
    "]+"
)


def _flag_foreign_script(text: str, notes: list[str]) -> None:
    """
    Флагва символи от чужда писменост в текста — само бележка, не пипа думата: коя точно
    част е повредена (една дума, цяло изречение) е преценка за човек, не механично изтриване.
    """
    found = sorted(set(_FOREIGN_SCRIPT_RE.findall(text)))
    if found:
        notes.append(
            f"Открити са символи от чужда писменост в текста ({' '.join(found)}) — вероятно "
            f"generation грешка на модела, не съзнателно добавен текст. Проверете и коригирайте "
            f"засегнатите думи преди публикуване."
        )


def _opening_has_a_fact(text: str) -> bool:
    """Корпусът иска отваряне с число, дата или място. Числото и месецът са проверими."""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    lowered = first_line.lower()
    return bool(_DIGIT_RE.search(first_line)) or any(month in lowered for month in MONTHS)


def enforce(
    text: str,
    rules: FormatRules,
    *,
    brief_text: str = "",
    has_photos: bool = True,
    founded_year: int | None = None,
    today: date | None = None,
) -> FormattingResult:
    """Прилага механичните правила и връща поправения текст + какво е поправено/остава."""
    fixes: list[str] = []
    notes: list[str] = []
    working = text

    # --- първо: автоматични поправки, приложени в ред (всяка мутира `working`)
    if rules.hashtags_forbidden:
        working = _strip_hashtags(working, fixes)
    if rules.enforces_emoji:
        working = _strip_disallowed_emoji(working, rules, fixes)
        working = _trim_excess_emoji(working, rules, fixes)
    working = _replace_unknown_urls(working, rules, brief_text, fixes)
    working = _strip_placeholder_closing(working, fixes)
    working = _strip_fake_photo_credit(working, has_photos, fixes)
    working = _strip_meta_disclaimers(working, notes)
    if rules.enforces_emoji and rules.emoji_min is not None:
        working = _add_missing_thematic_emoji(working, rules, fixes)
    working = _tidy(working)  # последно — маха следите от всички изтривания по-горе

    # --- нататък само констатации: поправката им е писане, не чистене
    words = count_words(working)
    if rules.words_min is not None and words < rules.words_min:
        notes.append(f"Постът е {words} думи, под минимума {rules.words_min} от knowledge/voice/tone.md.")
    if rules.words_max is not None and words > rules.words_max:
        notes.append(f"Постът е {words} думи, над максимума {rules.words_max} от knowledge/voice/tone.md.")

    if rules.enforces_emoji and rules.emoji_min is not None:
        emoji_count = len(extract_emoji(working))
        if emoji_count < rules.emoji_min:
            notes.append(
                f"Постът има {emoji_count} емоджи, под {rules.emoji_min} от корпуса — "
                f"добавете тематични от {' '.join(sorted(rules.allowed_emoji))}."
            )

    if rules.opening_needs_fact and not _opening_has_a_fact(working):
        notes.append(
            "Първият ред няма число, дата или месец. Корпусът иска отваряне с конкретен факт, "
            "а първите ~140 знака са единственото, което се вижда преди 'виж повече'."
        )

    _flag_leva_currency(working, notes)
    _flag_impossible_dates(working, notes)
    _flag_foreign_script(working, notes)
    _flag_anniversary_mismatch(working, founded_year, today or date.today(), notes)

    return FormattingResult(text=working, fixes=fixes, notes=notes, unenforceable=list(rules.unparsed))
