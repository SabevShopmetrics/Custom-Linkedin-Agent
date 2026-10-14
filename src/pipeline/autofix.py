"""
Стъпка [8] — самопоправка след финалната проверка (research-plan-38).

Досега финалният Критик [7] само ДОКЛАДВАШЕ: недоказаните твърдения, които Post II и
Консолидаторът не бяха махнали, стигаха до човека като бележки („13 бележки от проверката")
и човекът трябваше сам да ги чете и трие. Тук агентът ги маха сам — ЕДНА лека редакция
(writer.revise_post) с тясна молба „махни само тези", после кодова защита:

  - редакцията не бива да внася нови числа или собствени имена, които ги няма нито в
    предишния текст, нито в брифа → иначе се отхвърля и постът остава какъвто беше;
  - не бива да губи число или име, което е и в брифа (истински факт);
  - не бива да изреже повече от половината текст.

Поправя се само това, което е механично ясно и е ЦИТАТ от текста: недоказано твърдение и
твърдение, което брифът сам изразява като догадка. Само за цитат може да се провери с код
дали наистина е махнат. Нарушенията на правила („rule_violations") НЕ влизат: моделът пише в
тях описание, не цитат, и на живо (2026-10-14, бриф 04) нарече стилова бележка „нарушено
правило" — кодът я отчете като „махната", без да има какво да се маха. Тон, правила и GDPR
остават за човека (и за Post II, както досега).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.llm.schema import CritiqueSchema, FactSource

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
# Собствено име ≈ дума с главна буква, която НЕ е в началото на изречение.
_NAME_RE = re.compile(r"(?<![.!?\n]\s)(?<!^)\b([A-ZА-Я][\w\-]{2,})", re.UNICODE)
_WORD_RE = re.compile(r"[\wА-Яа-я]{4,}", re.UNICODE)
_ANY_WORD_RE = re.compile(r"[\w\-]{2,}", re.UNICODE)
_HEDGE_RE = re.compile(r"очаква|предварителн|вероятно|планира|предвижда", re.IGNORECASE)


@dataclass
class Fixable:
    kind: str  # "unverified" | "hedged"
    claim: str
    note: str  # бележката, както е в review_notes — за да се махне от списъка, ако е поправено


@dataclass
class AutofixResult:
    applied: bool
    text: str
    fixed: list[Fixable] = field(default_factory=list)
    reason: str = ""  # защо НЕ е приложена (за run.log)


def fixable_items(critique: CritiqueSchema) -> list[Fixable]:
    """Находките от [7], които агентът може да поправи сам. Текстът на `note` огледален на critic.review_notes."""
    items: list[Fixable] = []
    for fact in critique.facts:
        if fact.source is FactSource.UNVERIFIED:
            items.append(Fixable(
                "unverified", fact.claim, f"Недоказано твърдение: \"{fact.claim}\" ({fact.note or 'няма източник'})"
            ))
        elif fact.hedged:
            items.append(Fixable(
                "hedged", fact.claim,
                f"Хеджирано твърдение от брифа (изразено като предположение, не като "
                f"потвърден факт): \"{fact.claim}\" ({fact.note or 'няма бележка'})",
            ))
    return items


def instruction_for(items: list[Fixable]) -> str:
    lines = [
        "Финалната проверка намери неща, които не бива да излизат. Поправи САМО тях:",
    ]
    for item in items:
        if item.kind == "unverified":
            lines.append(f"- Махни твърдението (няма го в брифа): \"{item.claim}\"")
        else:
            lines.append(
                f"- Брифът не е сигурен в това — смекчи го („очаква се“, „по предварителна "
                f"информация“) или го махни: \"{item.claim}\""
            )
    lines.append(
        "Ако твърдението съдържа и истински факт от брифа (име, число, дата, място), ЗАПАЗИ факта "
        "и махни само добавката — напр. „Специален гост беше X“ → „Гост-лектор беше X“, ако брифът "
        "казва „гост-лектор“. Не добавяй нищо ново — нито факт, нито число, нито име. Не "
        "пренаписвай останалото. Върни целия пост."
    )
    return "\n".join(lines)


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUMBER_RE.findall(text)}


def _names(text: str) -> set[str]:
    return {m.lower() for m in _NAME_RE.findall(text)}


def new_specifics(old: str, new: str, brief: str) -> list[str]:
    """Числа и собствени имена в `new`, които ги няма нито в `old`, нито в брифа."""
    known_numbers = _numbers(old) | _numbers(brief)
    known_words = {w.lower() for w in _ANY_WORD_RE.findall(old + "\n" + brief)}
    added = [n for n in _numbers(new) if n not in known_numbers]
    added += [name for name in _names(new) if name not in known_words]
    return sorted(set(added))


def lost_brief_facts(old: str, new: str, brief: str) -> list[str]:
    """Числа и имена, които са в брифа И в стария текст, но изчезнаха от новия — загубен истински факт."""
    new_words = {w.lower() for w in _ANY_WORD_RE.findall(new)}
    brief_words = {w.lower() for w in _ANY_WORD_RE.findall(brief)}
    lost = [n for n in _numbers(old) & _numbers(brief) if n not in _numbers(new)]
    lost += [name for name in _names(old) if name in brief_words and name not in new_words]
    return sorted(set(lost))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def still_present(claim: str, text: str, old: str | None = None) -> bool:
    """
    Дали твърдението още стои. Ако Критикът го е цитирал дословно (има го в стария текст) —
    проверява точния цитат: „Специален гост беше X“ → „Гост-лектор беше X“ е поправка, макар че
    X остава. Иначе (преразказ) — груба мярка: ≥ 60 % от смислените думи са в текста.
    """
    if old is not None and _norm(claim) and _norm(claim) in _norm(old):
        return _norm(claim) in _norm(text)
    words = [w.lower() for w in _WORD_RE.findall(claim)]
    if not words:
        return claim.strip().lower() in text.lower()
    haystack = text.lower()
    hits = sum(1 for w in words if w in haystack)
    return hits / len(words) >= 0.6


def apply(old_text: str, new_text: str, brief: str, items: list[Fixable]) -> AutofixResult:
    """Кодовата защита около редакцията — решава дали да се приеме."""
    new_text = (new_text or "").strip()
    if not new_text:
        return AutofixResult(False, old_text, reason="празен отговор")
    if len(new_text.split()) < 0.5 * len(old_text.split()):
        return AutofixResult(False, old_text, reason="редакцията изряза повече от половината текст")
    added = new_specifics(old_text, new_text, brief)
    if added:
        return AutofixResult(False, old_text, reason=f"редакцията внесе нови подробности: {', '.join(added[:5])}")
    # Живо 2026-10-14 (бриф 02): Критикът възрази само срещу „Специален“ гост, а редакцията махна
    # цялото изречение — заедно с истинския лектор от брифа.
    lost = lost_brief_facts(old_text, new_text, brief)
    if lost:
        return AutofixResult(False, old_text, reason=f"редакцията махна факт от брифа: {', '.join(lost[:5])}")
    newly_hedged = len(_HEDGE_RE.findall(new_text)) > len(_HEDGE_RE.findall(old_text))
    fixed = [
        item for item in items
        if not still_present(item.claim, new_text, old_text) or (item.kind == "hedged" and newly_hedged)
    ]
    if not fixed:
        return AutofixResult(False, old_text, reason="нищо от исканото не беше махнато")
    return AutofixResult(True, new_text, fixed=fixed)
