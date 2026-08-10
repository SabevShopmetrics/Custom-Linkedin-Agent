"""
knowledge/ трябва да работи изцяло празно (агентът не бива да гърми), да разграничава
null (полето съществува, но не е потвърдено) от липсващ файл, и да докладва кои
източници реално са налични — за Критика и за `cli check-knowledge`.
"""

from __future__ import annotations

from pathlib import Path

from src.brand.loader import audit_knowledge, find_null_paths, load_brand_knowledge


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_fully_empty_knowledge_dir_does_not_crash(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.stable_context == ""
    assert knowledge.facts is None
    assert knowledge.facts_available is False
    assert knowledge.members == []
    assert knowledge.good_examples == []


def test_stable_context_includes_only_usable_sections_in_order(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(knowledge_dir / "identity/about.md", "Мисия на клъстера.")
    _write(knowledge_dir / "voice/tone.md", "Топъл тон.")
    _write(knowledge_dir / "rules/forbidden.md", "Не лъжем.")
    _write(knowledge_dir / "identity/history.md", "   ")  # само whitespace — третира се като празен

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.stable_context.index("МИСИЯ И ВИЗИЯ") < knowledge.stable_context.index("ТОН")
    assert knowledge.stable_context.index("ТОН") < knowledge.stable_context.index("ЗАБРАНЕНО")
    assert "ИСТОРИЯ" not in knowledge.stable_context  # празен файл не влиза
    assert "Мисия на клъстера." in knowledge.stable_context


def test_good_examples_are_appended_to_stable_context_bad_examples_are_not(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(knowledge_dir / "examples/good/01.md", "Добър пример за пост.")
    _write(knowledge_dir / "examples/good/_template.md", "шаблон, не се брои")
    _write(knowledge_dir / "examples/bad/01.md", "Лош пример за пост.")

    knowledge = load_brand_knowledge(knowledge_dir)

    assert "Добър пример за пост." in knowledge.stable_context
    assert "шаблон, не се брои" not in knowledge.stable_context
    assert "Лош пример за пост." not in knowledge.stable_context  # bad examples не влизат в стабилния контекст
    assert knowledge.good_examples == ["Добър пример за пост."]
    assert knowledge.bad_examples == ["Лош пример за пост."]


def test_facts_yaml_null_is_distinct_from_missing_key(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "identity/facts.yaml",
        """
organization:
  name_bg: "Тест"
members:
  total: null
""",
    )

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.facts_available is True
    assert knowledge.facts["organization"]["name_bg"] == "Тест"
    assert "total" in knowledge.facts["members"]  # ключът съществува
    assert knowledge.facts["members"]["total"] is None  # но стойността е непотвърдена


def test_missing_facts_yaml_means_not_available(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.facts_available is False
    assert knowledge.facts is None


def test_members_yaml_parses_list(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "members/members.yaml",
        """
members:
  - name: "Zariba"
    type: "фирма"
    mention_allowed: true
  - name: "ВВМУ"
    type: "университет"
    mention_allowed: null
""",
    )

    knowledge = load_brand_knowledge(knowledge_dir)

    assert len(knowledge.members) == 2
    assert knowledge.members[0]["mention_allowed"] is True
    assert knowledge.members[1]["mention_allowed"] is None


def test_find_null_paths_walks_nested_dicts_and_lists():
    data = {
        "a": 1,
        "b": None,
        "c": {"d": None, "e": "ok"},
        "f": [{"g": None}, {"g": "ok"}],
    }

    paths = find_null_paths(data)

    assert set(paths) == {"b", "c.d", "f[0].g"}


def test_audit_knowledge_reports_missing_files_and_null_facts(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(knowledge_dir / "identity/about.md", "нещо")
    _write(knowledge_dir / "identity/facts.yaml", "organization:\n  founded: null\n")

    result = audit_knowledge(knowledge_dir)

    about_entry = next(f for f in result["files"] if f.path == "identity/about.md")
    tone_entry = next(f for f in result["files"] if f.path == "voice/tone.md")
    assert about_entry.usable is True
    assert tone_entry.exists is False

    assert result["facts_available"] is True
    assert "organization.founded" in result["facts_null_fields"]
    assert result["members_count"] == 0


def test_audit_knowledge_surfaces_member_notes_for_check_knowledge_command(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "members/members.yaml",
        """
members:
  - name: "Devia Bulgaria"
    mention_allowed: null
    notes: "нов член 2026, липсва на сайта"
""",
    )

    result = audit_knowledge(knowledge_dir)

    assert len(result["members_with_notes"]) == 1
    assert result["members_with_notes"][0]["name"] == "Devia Bulgaria"
