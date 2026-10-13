"""
Зарежда цялата knowledge/ структура (identity, voice, rules, strategy, members,
examples) в един обект. Липсващ или празен файл никога не е грешка — само
предупреждение, записано в audit-а, което Критикът и `cli check-knowledge` използват,
за да знаят срещу какво реално могат да сверяват.

Знанието се разделя на стабилна част (identity/voice/rules/strategy — еднаква при
всяко извикване към даден модел) и променлива част (брифът, снимките). Стабилната
част се подава в системния промпт, променливата — в user съобщението, за да работи
кеширането на префикса на моделния доставчик.

Две правила за това КАКВО влиза в промпта, въведени от план 11, Фаза 0 (виж
docs/research-plans/research-plan-11-text-quality.md, находки 2.4 и 2.5):

  1. HTML коментарите не влизат. Те са бележки за човека, който редактира корпуса
     („<!-- TODO: попълва се от клъстера -->", „какво трябва да съдържа този файл").
     Файл, в който след махането на коментарите не остава нищо, е ПРАЗЕН — дори да е
     500 байта на диска. Дотогава `rules/forbidden.md` и `voice/vocabulary.md` се
     отчитаха като налични и стигаха до модела като секции, описващи какво би трябвало
     да съдържат — включително думите „предпочитани хаштагове" в секция ТЕРМИНОЛОГИЯ,
     докато ФОРМАТИ два абзаца по-долу забранява хаштаговете. Противоречие в един промпт.
  2. От еталонните примери влиза само секцията „## Текст на поста", и само от файловете,
     които са дословен пост. Дотогава влизаше целият файл — 11 percent-encoded LinkedIn
     URL-а, 12 блока „Защо е добър пример" (анализ за човек) и 7 дисклеймъра „Обобщение,
     НЕ пълен оригинален текст". Моделът се учи по подражание и подражаваше на обобщения,
     които завършват с „40 реакции." или „Споменава се, че…" — изречения, които никога не
     са били в пост.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.brand.format_rules import FormatRules, align_with_examples, parse_format_rules

DEFAULT_KNOWLEDGE_DIR = Path("knowledge")

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Заглавието в шаблона на еталонните примери (knowledge/examples/good/_template.md), под
# което стои самият текст на поста. Всичко останало в такъв файл (линк към оригинала,
# „Защо е добър пример") е за човека, не за модела.
_POST_TEXT_HEADING = "## Текст на поста"

# Маркерът, с който файловете сами се обявяват за обобщение, а не за дословен пост. Пише
# се в HTML коментар в началото на файла — виж напр. 08-technostart-ai-edition.md.
_SUMMARY_MARKERS = ("Обобщение, НЕ пълен", "обобщение, не пълен")

# (относителен път, заглавие в промпта) — реда тук определя реда в stable_context
_MARKDOWN_SECTIONS: list[tuple[str, str]] = [
    ("identity/about.md", "МИСИЯ И ВИЗИЯ"),
    ("identity/history.md", "ИСТОРИЯ"),
    ("voice/tone.md", "ТОН"),
    ("voice/formats.md", "ФОРМАТИ"),
    ("voice/vocabulary.md", "ТЕРМИНОЛОГИЯ"),
    ("rules/forbidden.md", "ЗАБРАНЕНО"),
    ("rules/gdpr.md", "GDPR"),
    ("rules/legal.md", "ПРАВНИ ИЗИСКВАНИЯ"),
    ("strategy/goals.md", "ЦЕЛИ"),
    ("strategy/audiences.md", "АУДИТОРИЯ"),
    ("strategy/pillars.md", "РУБРИКИ"),
]

# Файлове, чието съществуване/празнота одитираме отделно от YAML/examples по-долу.
_AUDITED_FILES = [path for path, _ in _MARKDOWN_SECTIONS]


@dataclass
class KnowledgeAudit:
    """Статус на един файл от knowledge/ — за check-knowledge и за реда какво реално да се сверява."""

    path: str
    exists: bool
    empty: bool  # съществува, но е празен/само whitespace/само HTML коментари, или YAML е {}/[]

    @property
    def usable(self) -> bool:
        return self.exists and not self.empty


@dataclass
class BrandKnowledge:
    """Целият заредена корпус — резултатът от load_brand_knowledge(), подаван на всяка pipeline стъпка."""

    stable_context: str
    facts: dict[str, Any] | None
    facts_available: bool
    members: list[dict[str, Any]]
    good_examples: list[str]  # САМО дословни постове — те са еталонът за подражание
    bad_examples: list[str]
    audit: list[KnowledgeAudit]
    # Механичните правила, извлечени от същите файлове — виж src/brand/format_rules.py.
    # Стоят тук, а не в pipeline кода, защото са част от корпуса: клъстерът ги променя в
    # tone.md/formats.md, не в Python.
    format_rules: FormatRules = field(default_factory=FormatRules)
    # Файлове в examples/good/, които сами се обявяват за обобщение, а не за дословен пост.
    # НЕ влизат в stable_context — стилът им (напр. „40 реакции.", „Споменава се, че…") е
    # точно това, което не искаме да бъде подражавано. Пазят се, за да може
    # `cli check-knowledge` да каже колко от корпуса реално е използваем като еталон.
    summary_examples: list[str] = field(default_factory=list)


def strip_comments(text: str) -> str:
    """
    Маха HTML коментарите и остатъчните празни редове след тях.

    Коментарът в knowledge/ е бележка за човека, който поддържа корпуса, не съдържание.
    Файл, от който след това не остава нищо, е празен по същество — виж докстринга на
    модула, правило 1.
    """
    without_comments = _HTML_COMMENT_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", without_comments).strip()


def _read_text(path: Path) -> str:
    """Съдържанието на файл, готово за промпт: без HTML коментари, без обграждащи празни редове."""
    if not path.exists():
        return ""
    return strip_comments(path.read_text(encoding="utf-8"))


def _audit_markdown(knowledge_dir: Path) -> list[KnowledgeAudit]:
    audits = []
    for rel_path in _AUDITED_FILES:
        path = knowledge_dir / rel_path
        exists = path.exists()
        empty = (not exists) or not _read_text(path)
        audits.append(KnowledgeAudit(path=rel_path, exists=exists, empty=empty))
    return audits


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    return yaml.safe_load(text)


def extract_post_text(raw_text: str) -> str:
    """
    Изважда само текста на поста от файл-пример.

    Ако файлът следва шаблона (`## Текст на поста`), се взима точно тази секция — не
    линкът към оригинала (percent-encoded URL, който няма работа в промпт) и не
    „Защо е добър пример" (анализ, писан за човек). Файл без заглавието се взима
    целият, за да продължат да работят примери, дописани на ръка без шаблона.
    """
    text = strip_comments(raw_text)
    index = text.find(_POST_TEXT_HEADING)
    if index == -1:
        return text

    after = text[index + len(_POST_TEXT_HEADING) :]
    next_heading = re.search(r"^## ", after, re.MULTILINE)
    section = after[: next_heading.start()] if next_heading else after
    return section.strip()


def _is_summary(raw_text: str) -> bool:
    """Файлът сам се обявява за обобщение, а не за дословно извлечен пост."""
    return any(marker in raw_text for marker in _SUMMARY_MARKERS)


def _load_examples(examples_dir: Path) -> tuple[list[str], list[str]]:
    """Връща (дословни постове, обобщения) — само първите са еталон за подражание."""
    if not examples_dir.is_dir():
        return [], []

    verbatim: list[str] = []
    summaries: list[str] = []
    for md_file in sorted(examples_dir.glob("*.md")):
        if md_file.name.startswith("_"):  # _template.md не е реален пример
            continue
        raw = md_file.read_text(encoding="utf-8")
        post_text = extract_post_text(raw)
        if not post_text:
            continue
        (summaries if _is_summary(raw) else verbatim).append(post_text)
    return verbatim, summaries


def load_brand_knowledge(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> BrandKnowledge:
    """
    Чете целия корпус (identity/voice/rules/strategy текстове, facts.yaml, members.yaml,
    examples/) и сглобява stable_context — единственият системен промпт-фрагмент, който
    Планер/Писател/Критик получават за организацията. Липсващ или празен файл не гърми,
    само не влиза в резултата (виж KnowledgeAudit по-горе).
    """
    # Стъпка 1: markdown секциите (about/history/tone/formats/...) — реда в _MARKDOWN_SECTIONS
    # определя реда в промпта; само usable файлове (виж strip_comments) влизат в blocks.
    audit = _audit_markdown(knowledge_dir)

    blocks: list[str] = []
    for (rel_path, title), knowledge_audit in zip(_MARKDOWN_SECTIONS, audit):
        if knowledge_audit.usable:
            blocks.append(f"## {title}\n\n{_read_text(knowledge_dir / rel_path)}")

    # Стъпка 2: identity/facts.yaml — машинно проверимите факти (ЕИК, адрес, програми...).
    facts_raw = _load_yaml(knowledge_dir / "identity/facts.yaml")
    facts_available = facts_raw is not None
    audit.append(
        KnowledgeAudit(path="identity/facts.yaml", exists=(knowledge_dir / "identity/facts.yaml").exists(), empty=not facts_available)
    )

    # Стъпка 3: members/members.yaml — списъкът членове, вкл. mention_allowed за GDPR проверката.
    members_raw = _load_yaml(knowledge_dir / "members/members.yaml") or {}
    members = members_raw.get("members") or [] if isinstance(members_raw, dict) else []
    audit.append(
        KnowledgeAudit(
            path="members/members.yaml",
            exists=(knowledge_dir / "members/members.yaml").exists(),
            empty=len(members) == 0,
        )
    )

    # Стъпка 4: еталонни примери — разделени на дословни (влизат в промпта) и обобщения
    # (само за одит, виж _load_examples и докстринга на модула, правило 2).
    good_examples, summary_examples = _load_examples(knowledge_dir / "examples/good")
    bad_examples, _ = _load_examples(knowledge_dir / "examples/bad")
    audit.append(KnowledgeAudit(path="examples/good/", exists=True, empty=len(good_examples) == 0))
    audit.append(KnowledgeAudit(path="examples/bad/", exists=True, empty=len(bad_examples) == 0))

    if good_examples:
        examples_text = "\n\n---\n\n".join(good_examples)
        blocks.append(
            "## ЕТАЛОННИ ПРИМЕРИ — дословни публикувани постове\n\n"
            "Това са реални постове на страницата, дума по дума. Те са мярката за глас, "
            "плътност на факти и дължина.\n\n"
            f"{examples_text}"
        )

    # Стъпка 5: сглобяване на финалния системен промпт-фрагмент от всички секции по-горе.
    stable_context = "\n\n".join(blocks)

    facts = facts_raw if isinstance(facts_raw, dict) else None

    return BrandKnowledge(
        stable_context=stable_context,
        facts=facts,
        facts_available=facts_available,
        members=members,
        good_examples=good_examples,
        bad_examples=bad_examples,
        audit=audit,
        # Г1 (research-plan-36): правилата се разширяват до дословните постове — виж
        # format_rules.align_with_examples защо.
        format_rules=align_with_examples(
            parse_format_rules(
                _read_text(knowledge_dir / "voice/tone.md"),
                _read_text(knowledge_dir / "voice/formats.md"),
                facts,
            ),
            good_examples,
        ),
        summary_examples=summary_examples,
    )


def find_null_paths(data: Any, prefix: str = "") -> list[str]:
    """
    Рекурсивно намира всички пътища до YAML стойности null — за facts.yaml, null е
    семантично „знаем, че съществува, но не е потвърдено", различно от липсващ ключ.
    """
    paths: list[str] = []
    if data is None:
        # Базов случай на рекурсията — стигнахме до самата null стойност, връщаме пътя до нея.
        return [prefix] if prefix else []

    if isinstance(data, dict):
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(find_null_paths(value, child_prefix))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            paths.extend(find_null_paths(item, f"{prefix}[{idx}]"))
    # друг тип (str/int/bool/...) — има стойност, не е null, нищо не се добавя

    return paths


def audit_knowledge(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> dict[str, Any]:
    """
    Пълен одит на knowledge/ за `python -m src.cli check-knowledge`: кои файлове
    съществуват/са празни, кои полета в facts.yaml са null, колко членове/примери има.
    """
    knowledge = load_brand_knowledge(knowledge_dir)
    facts_null_fields = find_null_paths(knowledge.facts) if knowledge.facts else []
    members_with_notes = [m for m in knowledge.members if m.get("notes")]

    return {
        "files": knowledge.audit,
        "facts_available": knowledge.facts_available,
        "facts_null_fields": facts_null_fields,
        "members_count": len(knowledge.members),
        "members_mentionable_count": sum(1 for m in knowledge.members if m.get("mention_allowed") is True),
        "members_with_notes": members_with_notes,
        "good_examples_count": len(knowledge.good_examples),
        # Колко от examples/good/ са обобщения, а не дословни постове. Не са грешка, но не
        # са и еталон — не влизат в промпта (виж докстринга на модула, правило 2).
        "summary_examples_count": len(knowledge.summary_examples),
        "bad_examples_count": len(knowledge.bad_examples),
        # research-plan-36, Г4: промптът беше нараснал от 6.3k (след почистването в план 11)
        # до ~15k знака, главно от бележки за разработчици с дати в tone.md/forbidden.md.
        "stable_context_chars": len(knowledge.stable_context),
        "dated_notes_in_prompt": dated_notes_in_prompt(knowledge.stable_context),
    }


# Курсивна бележка от рода на „*Добавено 2026-08-27 по живо тестване…" — история за
# разработчика, която трябва да е в HTML коментар, не в текста, който моделът чете.
_DATED_NOTE_RE = re.compile(
    r"^\s*\*?(?:Добавено|Допълнено|Коригирано|Разширено|Уточнение)[^\n]*20\d\d-\d\d-\d\d", re.MULTILINE
)


def dated_notes_in_prompt(stable_context: str) -> list[str]:
    """Редове с датирани бележки за разработчици, които стигат до промпта (виж check-knowledge)."""
    return [match.group(0).strip()[:90] for match in _DATED_NOTE_RE.finditer(stable_context)]
