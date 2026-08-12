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


@dataclass
class FormattingResult:
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
    hashtags = find_hashtags(text)
    if not hashtags:
        return text
    for tag in hashtags:
        text = text.replace(tag, "")
    fixes.append(f"Премахнати хаштагове ({', '.join(hashtags)}) — гласът на клъстера не ги ползва.")
    return text


def _strip_disallowed_emoji(text: str, rules: FormatRules, fixes: list[str]) -> str:
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


def _opening_has_a_fact(text: str) -> bool:
    """Корпусът иска отваряне с число, дата или място. Числото и месецът са проверими."""
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    lowered = first_line.lower()
    return bool(_DIGIT_RE.search(first_line)) or any(month in lowered for month in MONTHS)


def enforce(text: str, rules: FormatRules, *, brief_text: str = "") -> FormattingResult:
    """Прилага механичните правила и връща поправения текст + какво е поправено/остава."""
    fixes: list[str] = []
    notes: list[str] = []
    working = text

    if rules.hashtags_forbidden:
        working = _strip_hashtags(working, fixes)
    if rules.enforces_emoji:
        working = _strip_disallowed_emoji(working, rules, fixes)
        working = _trim_excess_emoji(working, rules, fixes)
    working = _replace_unknown_urls(working, rules, brief_text, fixes)
    working = _tidy(working)

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

    return FormattingResult(text=working, fixes=fixes, notes=notes, unenforceable=list(rules.unparsed))
