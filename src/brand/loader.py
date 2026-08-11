"""
Зарежда цялата knowledge/ структура (identity, voice, rules, strategy, members,
examples) в един обект. Липсващ или празен файл никога не е грешка — само
предупреждение, записано в audit-а, което Критикът и `cli check-knowledge` използват,
за да знаят срещу какво реално могат да сверяват.

Знанието се разделя на стабилна част (identity/voice/rules/strategy — еднаква при
всяко извикване към даден модел) и променлива част (брифът, снимките). Стабилната
част се подава в системния промпт, променливата — в user съобщението, за да работи
кеширането на префикса на моделния доставчик.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.brand.format_rules import FormatRules, parse_format_rules

DEFAULT_KNOWLEDGE_DIR = Path("knowledge")

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
_AUDITED_YAML_FILES = ["identity/facts.yaml", "members/members.yaml"]


@dataclass
class KnowledgeAudit:
    path: str
    exists: bool
    empty: bool  # съществува, но е празен/само whitespace, или YAML е {}/[]

    @property
    def usable(self) -> bool:
        return self.exists and not self.empty


@dataclass
class BrandKnowledge:
    stable_context: str
    facts: dict[str, Any] | None
    facts_available: bool
    members: list[dict[str, Any]]
    good_examples: list[str]
    bad_examples: list[str]
    audit: list[KnowledgeAudit]
    # Механичните правила, извлечени от същите файлове — виж src/brand/format_rules.py.
    # Стоят тук, а не в pipeline кода, защото са част от корпуса: клъстерът ги променя в
    # tone.md/formats.md, не в Python.
    format_rules: FormatRules = field(default_factory=FormatRules)

    def missing_sources_summary(self) -> str:
        missing = [a.path for a in self.audit if not a.usable]
        if not missing:
            return "(всички източници на знание са налични)"
        return "Липсващи/празни източници на знание: " + ", ".join(missing)


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


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


def _load_examples(examples_dir: Path) -> list[str]:
    if not examples_dir.is_dir():
        return []
    texts = []
    for md_file in sorted(examples_dir.glob("*.md")):
        if md_file.name.startswith("_"):  # _template.md не е реален пример
            continue
        content = _read_text(md_file)
        if content:
            texts.append(content)
    return texts


def load_brand_knowledge(knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR) -> BrandKnowledge:
    audit = _audit_markdown(knowledge_dir)

    blocks: list[str] = []
    for (rel_path, title), knowledge_audit in zip(_MARKDOWN_SECTIONS, audit):
        if knowledge_audit.usable:
            blocks.append(f"## {title}\n\n{_read_text(knowledge_dir / rel_path)}")

    facts_raw = _load_yaml(knowledge_dir / "identity/facts.yaml")
    facts_available = facts_raw is not None
    audit.append(
        KnowledgeAudit(path="identity/facts.yaml", exists=(knowledge_dir / "identity/facts.yaml").exists(), empty=not facts_available)
    )

    members_raw = _load_yaml(knowledge_dir / "members/members.yaml") or {}
    members = members_raw.get("members") or [] if isinstance(members_raw, dict) else []
    audit.append(
        KnowledgeAudit(
            path="members/members.yaml",
            exists=(knowledge_dir / "members/members.yaml").exists(),
            empty=len(members) == 0,
        )
    )

    good_examples = _load_examples(knowledge_dir / "examples/good")
    bad_examples = _load_examples(knowledge_dir / "examples/bad")
    audit.append(KnowledgeAudit(path="examples/good/", exists=True, empty=len(good_examples) == 0))
    audit.append(KnowledgeAudit(path="examples/bad/", exists=True, empty=len(bad_examples) == 0))

    if good_examples:
        examples_text = "\n\n---\n\n".join(good_examples)
        blocks.append(f"## ЕТАЛОННИ ПРИМЕРИ (добри)\n\n{examples_text}")

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
        format_rules=parse_format_rules(
            _read_text(knowledge_dir / "voice/tone.md"),
            _read_text(knowledge_dir / "voice/formats.md"),
            facts,
        ),
    )


def find_null_paths(data: Any, prefix: str = "") -> list[str]:
    """
    Рекурсивно намира всички пътища до YAML стойности null — за facts.yaml, null е
    семантично „знаем, че съществува, но не е потвърдено", различно от липсващ ключ.
    """
    paths: list[str] = []
    if data is None:
        return [prefix] if prefix else []

    if isinstance(data, dict):
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(find_null_paths(value, child_prefix))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            paths.extend(find_null_paths(item, f"{prefix}[{idx}]"))

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
        "members_with_notes": members_with_notes,
        "good_examples_count": len(knowledge.good_examples),
        "bad_examples_count": len(knowledge.bad_examples),
    }
