"""Тестове за src/pipeline/facts_pool.py — стъпка [1.5] на v5 (план 15). Без LLM извикване,
чист код: build() е детерминирано, тества се като всяка друга функция."""

from __future__ import annotations

from src.pipeline import facts_pool
from tests.conftest import make_brand_knowledge


def test_build_extracts_proper_nouns_and_numbers_from_briefing(tmp_path):
    knowledge = make_brand_knowledge()
    pool = facts_pool.build(
        tmp_path, "Утре в 10 часа в офиса на ИКТ Клъстер посрещаме гост от Software University.", knowledge
    )

    assert "Software University" in pool.briefing_claims or "Software" in " ".join(pool.briefing_claims)
    assert any("10" in claim for claim in pool.briefing_claims)
    assert (tmp_path / "facts_pool.json").exists()


def test_build_only_includes_mentionable_members(tmp_path):
    knowledge = make_brand_knowledge(
        members=[
            {"name": "Zariba EOOD", "mention_allowed": True},
            {"name": "Друга фирма", "mention_allowed": False},
        ]
    )
    pool = facts_pool.build(tmp_path, "Бриф без имена.", knowledge)

    assert pool.mentionable_members == ["Zariba EOOD"]


def test_build_is_resumable_without_force(tmp_path):
    knowledge = make_brand_knowledge()
    first = facts_pool.build(tmp_path, "Бриф версия 1 с 5 участника.", knowledge)
    # Втори бриф текст, но force=False — очаква се СТАРИЯТ регистър да се препрочете,
    # не да се преизчисли (виж should_skip навсякъде другаде в конвейера).
    second = facts_pool.build(tmp_path, "Съвсем друг бриф с 99 участника.", knowledge)

    assert second.briefing_claims == first.briefing_claims


def test_build_force_recomputes(tmp_path):
    knowledge = make_brand_knowledge()
    facts_pool.build(tmp_path, "Бриф версия 1 с 5 участника.", knowledge)
    second = facts_pool.build(tmp_path, "Съвсем друг бриф с 99 участника.", knowledge, force=True)

    assert any("99" in claim for claim in second.briefing_claims)


def test_render_for_writer_lists_all_three_sources():
    pool = facts_pool.FactsPool(
        briefing_claims=["Варна", "10"],
        facts_yaml={"organization": {"name_bg": "ИКТ Клъстер – Варна"}},
        mentionable_members=["Zariba EOOD"],
    )
    rendered = facts_pool.render_for_writer(pool)

    assert "Варна" in rendered
    assert "ИКТ Клъстер" in rendered
    assert "Zariba EOOD" in rendered


def test_render_for_writer_handles_empty_pool_without_crashing():
    pool = facts_pool.FactsPool()
    rendered = facts_pool.render_for_writer(pool)

    assert "не съдържа" in rendered or "липсва" in rendered


def test_allowed_strings_includes_facts_yaml_dump():
    pool = facts_pool.FactsPool(facts_yaml={"organization": {"eik": "175578357"}})
    allowed = pool.allowed_strings()

    assert any("175578357" in s for s in allowed)
