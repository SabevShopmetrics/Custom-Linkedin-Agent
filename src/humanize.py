"""
Превежда вътрешните бележки на конвейера на език, разбираем за маркетинг екипа.

Защо отделен слой (research-plan-36, В2): бележките в `meta.json` / `run.log` са одитна следа
за разработчика — препращат към `members.yaml`, `mention_allowed`, `rules/forbidden.md`,
„Post II", „Media Picker". Показани дословно на маркетолог, те изглеждат като задачи, които
той не може да изпълни. Тук се сменя САМО какво вижда човекът на екрана и в preview.html;
суровият текст остава непроменен на диска.

Всяко правило връща или човешко изречение с конкретно действие, или None — „не показвай"
(вътрешна информация без действие за човека, напр. че синтезът е избрал едната чернова).
Непознат текст минава през `_soften_jargon` и се показва — по-добре леко технически, отколкото
изгубена бележка.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

_Q = r"['\"„“]"  # кавички, с които конвейерът огражда имена и твърдения


def _quoted(text: str) -> str:
    return f"„{text.strip()}“"


_RULES: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str | None]]] = [
    # --- лични данни / съгласие (critic.enforce_code_level_rules)
    (
        re.compile(rf"^\[GDPR\] Организацията {_Q}(.+?){_Q} е от брифа"),
        lambda m: (
            f"Споменавате {_quoted(m.group(1))}. Нямаме записано тяхно съгласие за публично "
            f"споменаване — уверете се, че е наред, преди да публикувате."
        ),
    ),
    (
        re.compile(rf"^\[GDPR\] Организацията {_Q}(.+?){_Q} е спомената в текста"),
        lambda m: (
            f"Постът споменава {_quoted(m.group(1))}, без да сте го написали в брифа. Нямаме "
            f"записано тяхно съгласие — махнете името или проверете дали е наред."
        ),
    ),
    (
        re.compile(rf"^\[GDPR\] Възможно лично име извън брифа: {_Q}(.+?){_Q}"),
        lambda m: (
            f"В текста има име {_quoted(m.group(1))}, което не е от вашия бриф — проверете дали "
            f"е вярно и дали човекът е съгласен да бъде споменат."
        ),
    ),
    (
        re.compile(rf"^\[GDPR\] Възможна непозната организация.*?: {_Q}(.+?){_Q}"),
        lambda m: (
            f"Споменава се {_quoted(m.group(1))} — не е в списъка с членове. Уверете се, че "
            f"имате съгласие да ги споменете публично."
        ),
    ),
    (
        re.compile(rf"^\[GDPR\] Пряко приписано изказване на {_Q}(.+?){_Q}"),
        lambda m: f"Постът цитира {_quoted(m.group(1))} — нужно е съгласието на човека да бъде цитиран.",
    ),
    (
        re.compile(r"^\[GDPR\] (.+)$", re.DOTALL),
        lambda m: f"Лични данни / съгласие: {_soften_jargon(m.group(1))}",
    ),
    # --- забранени теми
    (
        re.compile(r"^\[rules/forbidden\.md\] (.+)$", re.DOTALL),
        lambda m: f"Нарушава правилата на клъстера: {_soften_jargon(m.group(1))} Махнете или пренапишете тази част.",
    ),
    # --- факти
    (
        re.compile(rf"^Недоказано твърдение[^:]*: {_Q}(.+?){_Q}"),
        lambda m: f"Проверете този факт — не е от брифа: {_quoted(m.group(1))}",
    ),
    (
        re.compile(rf"^Хеджирано твърдение.*?: {_Q}(.+?){_Q}"),
        lambda m: (
            f"В брифа това е предположение, не сигурен факт — формулирайте го внимателно "
            f"или го махнете: {_quoted(m.group(1))}"
        ),
    ),
    # --- снимки
    (
        re.compile(rf"^Media Picker флагна {_Q}(.+?){_Q} като вероятно несъответстваща тематично на брифа(?:: (.+?))? — "),
        lambda m: (
            f"Снимката {_quoted(m.group(1))} може да не пасва на темата"
            + (f" ({m.group(2).strip()})" if m.group(2) else "")
            + " — проверете я."
        ),
    ),
    (
        re.compile(r"^Избрана снимка е маркирана .*непълнолетни"),
        lambda m: (
            "На избрана снимка вероятно има деца или непълнолетни — нужно е съгласие на "
            "родителите, преди да я публикувате."
        ),
    ),
    # --- състояния на целия пост
    (
        re.compile(r"^ЧУВСТВИТЕЛНА ТЕМА"),
        lambda m: "Чувствителна тема — прочетете поста внимателно и го дайте за одобрение преди публикуване.",
    ),
    (
        re.compile(r"^Брифът изглежда без достатъчно съдържание"),
        lambda m: (
            "Брифът е твърде кратък — текстът е само чернова. Добавете детайли: какво, кога, "
            "кой участва, какъв е резултатът."
        ),
    ),
    (
        re.compile(r"^ТЕМАТА НА БРИФА НАРУШАВА ПРАВИЛАТА"),
        lambda m: "Темата е от забранените по правилата на клъстера — нужен е друг повод за пост.",
    ),
    (
        re.compile(r"^(Вторият пост не беше написан|Консолидаторът не успя)"),
        lambda m: "Подобряването на текста не завърши (временен проблем с модела) — прочетете поста внимателно.",
    ),
    (
        re.compile(r"^Финалният текст има .* по-малко конкретни"),
        None,  # тази бележка вече е написана за човек (runner._fact_loss_note) — показва се дословно
    ),
    # --- вътрешна информация без действие за маркетолога
    (re.compile(r"^Консолидаторът върна .* дословно"), lambda m: None),
    (re.compile(r"^Препоръка на Критика:"), lambda m: None),
]

# Термини, които се изплъзват и в свободния текст на модела или в бележките на formatting.py.
_JARGON = [
    (re.compile(r"\bmembers\.yaml\b"), "списъка с членове"),
    (re.compile(r"\bfacts\.yaml\b"), "данните за клъстера"),
    (re.compile(r"\b(?:knowledge/)?rules/forbidden\.md\b"), "правилата на клъстера"),
    (re.compile(r"\b(?:knowledge/)?voice/(?:tone|vocabulary|formats)\.md\b"), "правилата за стил"),
    (re.compile(r"\bknowledge/voice/?"), "правилата за стил"),
    (re.compile(r"\bmention_allowed\b(?: не е потвърдено \(true\))?"), "съгласие"),
    (re.compile(r"\bMedia Picker\b"), "изборът на снимки"),
    (re.compile(r"\bPost II\b"), "втората версия"),
    (re.compile(r"\bPost I\b"), "черновата"),
    (re.compile(r"\bpost_ii?\.md\b"), "черновата"),
    (re.compile(r"\bgeneration грешка\b"), "грешка при генериране"),
    (re.compile(r"\bplaceholder\b"), "временен заместител"),
    (re.compile(r"\bCLAUDE\.md\b[^)]*"), "правилата на проекта"),
]


def _soften_jargon(text: str) -> str:
    for pattern, replacement in _JARGON:
        text = pattern.sub(replacement, text)
    return text.strip()


def humanize_note(note: str) -> str | None:
    """Една бележка → изречение за маркетолог, или None, ако няма какво да се покаже."""
    stripped = note.strip()
    for pattern, render in _RULES:
        match = pattern.search(stripped)
        if match:
            if render is None:
                return stripped
            return render(match)
    return _soften_jargon(stripped)


def humanize_notes(notes: Iterable[str]) -> list[str]:
    """Превежда и премахва празните/скритите, без повторения (запазва реда)."""
    seen: set[str] = set()
    result: list[str] = []
    for note in notes:
        text = humanize_note(note)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


STATUS_LABELS = {
    "ready": "Готов за публикуване",
    "ready_with_notes": "Готов — прегледайте бележките",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, "Готов — прегледайте бележките")
