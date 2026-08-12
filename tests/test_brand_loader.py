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


# --------------------------------------------------------------- план 11, Фаза 0
# Хигиена на входа: коментарите не са съдържание, а еталон е само дословен пост.
# Виж docs/research-plans/research-plan-11-text-quality.md, находки 2.4 и 2.5.


def test_file_with_only_html_comments_counts_as_empty(tmp_path):
    """
    Реалният случай, който наложи проверката: rules/forbidden.md и voice/vocabulary.md
    съдържаха само `<!-- какво трябва да съдържа този файл -->` + `<!-- TODO -->`, но
    одитът ги отчиташе като налични, защото файлът не е празен на диска. Моделът
    получаваше описанието като правило.
    """
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "rules/forbidden.md",
        "<!--\nКакво трябва да съдържа този файл: червени линии.\n-->\n<!-- TODO -->\n",
    )

    knowledge = load_brand_knowledge(knowledge_dir)
    entry = next(a for a in knowledge.audit if a.path == "rules/forbidden.md")

    assert entry.exists is True
    assert entry.empty is True
    assert entry.usable is False
    assert "ЗАБРАНЕНО" not in knowledge.stable_context
    assert "червени линии" not in knowledge.stable_context


def test_comments_are_stripped_from_sections_that_do_have_content(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(knowledge_dir / "voice/tone.md", "- Дължина: 60–150 думи.\n\n<!-- TODO: клъстерът ще допълни -->\n")

    knowledge = load_brand_knowledge(knowledge_dir)

    assert "Дължина: 60–150 думи." in knowledge.stable_context
    assert "TODO" not in knowledge.stable_context
    assert "<!--" not in knowledge.stable_context


def test_only_post_text_section_of_an_example_reaches_the_prompt(tmp_path):
    """
    Целият файл влизаше в промпта — заедно с percent-encoded LinkedIn URL и с блока
    „Защо е добър пример", който е анализ за човек, не текст за подражание.
    """
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "examples/good/01-praktiki.md",
        "<!-- Реален пост, пълен оригинален текст. -->\n\n"
        "## Линк към оригиналния пост\n\n"
        "https://www.linkedin.com/posts/ict-cluster-varna_%D0%BD%D0%B0-activity-747303\n\n"
        "## Защо е добър пример\n\n"
        "Показва как се съчетават гост и резултат за общността.\n\n"
        "## Текст на поста\n\n"
        "На 11 юни проведохме петото издание.\n",
    )

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.good_examples == ["На 11 юни проведохме петото издание."]
    assert "linkedin.com/posts" not in knowledge.stable_context
    assert "Защо е добър пример" not in knowledge.stable_context
    assert "На 11 юни проведохме петото издание." in knowledge.stable_context


def test_summary_examples_are_loaded_but_kept_out_of_the_prompt(tmp_path):
    """
    7 от 12 файла в examples/good/ сами се обявяват за обобщение („40 реакции.",
    „Споменава се, че…"). Като еталон за подражание те учат модела на стил, който никога
    не е бил в пост — затова се броят, но не влизат в stable_context.
    """
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "examples/good/08-technostart.md",
        "<!--\nОбобщение, НЕ пълен оригинален текст — само URL адресът е потвърден.\n-->\n\n"
        "## Текст на поста\n\n"
        "27 отбора. 76 участници. 40 реакции.\n",
    )
    _write(
        knowledge_dir / "examples/good/01-praktiki.md",
        "<!-- Реален пост, пълен оригинален текст. -->\n\n## Текст на поста\n\nДословен пост.\n",
    )

    knowledge = load_brand_knowledge(knowledge_dir)

    assert knowledge.good_examples == ["Дословен пост."]
    assert knowledge.summary_examples == ["27 отбора. 76 участници. 40 реакции."]
    assert "40 реакции" not in knowledge.stable_context
    assert "Дословен пост." in knowledge.stable_context


def test_audit_counts_summaries_separately_from_verbatim_examples(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    _write(
        knowledge_dir / "examples/good/a.md",
        "<!-- Обобщение, НЕ пълен оригинален текст -->\n## Текст на поста\n\nОбобщено.\n",
    )
    _write(knowledge_dir / "examples/good/b.md", "## Текст на поста\n\nДословно.\n")
    _write(
        knowledge_dir / "members/members.yaml",
        "members:\n  - name: 'Zariba'\n    mention_allowed: true\n  - name: 'ВВМУ'\n    mention_allowed: null\n",
    )

    result = audit_knowledge(knowledge_dir)

    assert result["good_examples_count"] == 1
    assert result["summary_examples_count"] == 1
    assert result["members_count"] == 2
    assert result["members_mentionable_count"] == 1
