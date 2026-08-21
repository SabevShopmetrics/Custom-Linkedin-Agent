"""Тестове за src/pipeline/verifier.py — стъпка [4] на v5 (план 15). Код, без LLM."""

from __future__ import annotations

from src.pipeline import facts_pool, verifier
from tests.conftest import make_brand_knowledge


def _pool(**overrides):
    base = dict(briefing_claims=["10", "Варна"], facts_yaml={}, mentionable_members=[])
    base.update(overrides)
    return facts_pool.FactsPool(**base)


def test_claim_covered_by_briefing_text_is_verified():
    knowledge = make_brand_knowledge()
    text = "Утре в 10 часа във Варна се провежда среща."
    result = verifier.check_candidate("post_1.md", text, briefing_text=text, pool=_pool(), knowledge=knowledge)

    assert all(c.verified for c in result.claims)
    assert result.unverified_count == 0


def test_claim_not_in_brief_or_pool_is_unverified():
    knowledge = make_brand_knowledge()
    briefing_text = "Утре имаме уъркшоп."
    text = "Утре имаме уъркшоп с водещ експерт от Software University в залата."
    result = verifier.check_candidate("post_1.md", text, briefing_text=briefing_text, pool=_pool(), knowledge=knowledge)

    assert result.unverified_count > 0
    unverified_claims = [c.claim for c in result.claims if not c.verified]
    assert any("Software" in c or "University" in c for c in unverified_claims)


def test_claim_covered_by_facts_yaml_is_verified():
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко събитие."
    pool = _pool(facts_yaml={"organization": {"eik": "175578357"}})
    text = "ЕИК-ът на клъстера е 175578357."
    result = verifier.check_candidate("post_1.md", text, briefing_text=briefing_text, pool=pool, knowledge=knowledge)

    assert any(c.claim == "175578357" for c in result.claims)
    assert all(c.verified for c in result.claims)


def test_gdpr_code_level_check_still_runs_on_candidates():
    """process-4.md: enforce_code_level_rules трябва да остане непокътнат и да се пуска и тук."""
    knowledge = make_brand_knowledge(members=[{"name": "Zariba EOOD", "mention_allowed": False}])
    briefing_text = "Среща с партньори."
    text = "Благодарим на Zariba EOOD за домакинството."
    result = verifier.check_candidate("post_1.md", text, briefing_text=briefing_text, pool=_pool(), knowledge=knowledge)

    assert any(v.rule == "gdpr" for v in result.rule_violations)
    assert not result.blocked  # GDPR сам по себе си не блокира — само "forbidden" го прави


def test_check_all_writes_facts_check_json(tmp_path):
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко."
    candidates = [("post_1.md", "Пост един."), ("post_2.md", "Пост два.")]
    results = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge)

    assert len(results) == 2
    assert (tmp_path / "facts_check.json").exists()


def test_check_all_resumable_without_force(tmp_path):
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко."
    candidates = [("post_1.md", "Пост един.")]
    first = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge)
    second = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge)

    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]
