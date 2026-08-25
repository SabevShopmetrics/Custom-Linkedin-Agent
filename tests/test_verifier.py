"""
Тестове за src/pipeline/verifier.py — стъпка [4] на v5 (план 15). Твърденията/GDPR остават
код, без LLM; rules/forbidden.md от 2026-08-25 минава през ЕДНА лека LLM проверка на кандидат
(critic.check_forbidden_rules) — затваря pre-selection пропуска, документиран в докстринга на
модула. FakeForbiddenClient подменя тази проверка тук, за да тестваме плъзгащата логика без
реална мрежа.
"""

from __future__ import annotations

from typing import Any

from src.llm.roles import Role
from src.llm.schema import ForbiddenCheckSchema, RuleViolation
from src.pipeline import facts_pool, verifier
from tests.conftest import make_brand_knowledge


def _pool(**overrides):
    base = dict(briefing_claims=["10", "Варна"], facts_yaml={}, mentionable_members=[])
    base.update(overrides)
    return facts_pool.FactsPool(**base)


class FakeForbiddenClient:
    """Връща предварително зададени rule_violations за check_forbidden_rules, без мрежа."""

    def __init__(self, violations: list[RuleViolation] | None = None) -> None:
        self._violations = violations or []
        self.calls = 0

    def complete(self, role: Role, prompt: str, *, system=None, images=None, schema=None) -> Any:
        assert role is Role.CRITIC
        assert schema is ForbiddenCheckSchema
        self.calls += 1
        return ForbiddenCheckSchema(rule_violations=list(self._violations))


def _client(violations: list[RuleViolation] | None = None) -> FakeForbiddenClient:
    return FakeForbiddenClient(violations)


def test_claim_covered_by_briefing_text_is_verified():
    knowledge = make_brand_knowledge()
    text = "Утре в 10 часа във Варна се провежда среща."
    result = verifier.check_candidate("post_1.md", text, briefing_text=text, pool=_pool(), knowledge=knowledge, client=_client())

    assert all(c.verified for c in result.claims)
    assert result.unverified_count == 0


def test_claim_not_in_brief_or_pool_is_unverified():
    knowledge = make_brand_knowledge()
    briefing_text = "Утре имаме уъркшоп."
    text = "Утре имаме уъркшоп с водещ експерт от Software University в залата."
    result = verifier.check_candidate(
        "post_1.md", text, briefing_text=briefing_text, pool=_pool(), knowledge=knowledge, client=_client()
    )

    assert result.unverified_count > 0
    unverified_claims = [c.claim for c in result.claims if not c.verified]
    assert any("Software" in c or "University" in c for c in unverified_claims)


def test_claim_covered_by_facts_yaml_is_verified():
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко събитие."
    pool = _pool(facts_yaml={"organization": {"eik": "175578357"}})
    text = "ЕИК-ът на клъстера е 175578357."
    result = verifier.check_candidate(
        "post_1.md", text, briefing_text=briefing_text, pool=pool, knowledge=knowledge, client=_client()
    )

    assert any(c.claim == "175578357" for c in result.claims)
    assert all(c.verified for c in result.claims)


def test_gdpr_code_level_check_still_runs_on_candidates():
    """process-4.md: enforce_code_level_rules трябва да остане непокътнат и да се пуска и тук."""
    knowledge = make_brand_knowledge(members=[{"name": "Zariba EOOD", "mention_allowed": False}])
    briefing_text = "Среща с партньори."
    text = "Благодарим на Zariba EOOD за домакинството."
    result = verifier.check_candidate(
        "post_1.md", text, briefing_text=briefing_text, pool=_pool(), knowledge=knowledge, client=_client()
    )

    assert any(v.rule == "gdpr" for v in result.rule_violations)
    assert not result.blocked  # GDPR сам по себе си не блокира — само "forbidden" го прави


def test_forbidden_rule_violation_from_llm_blocks_the_candidate():
    """
    Затвореният пропуск: rules/forbidden.md нарушение вече идва от check_forbidden_rules
    (лека LLM проверка), не само от кода — и вдига .blocked, точно както в v3.
    """
    knowledge = make_brand_knowledge()
    briefing_text = "Пост за нашето събитие."
    text = "Нашето събитие е много по-добро от конференцията на конкурента Х."
    client = _client([RuleViolation(rule="forbidden", detail="критика на конкретен конкурент")])

    result = verifier.check_candidate(
        "post_1.md", text, briefing_text=briefing_text, pool=_pool(), knowledge=knowledge, client=client
    )

    assert client.calls == 1
    assert any(v.rule == "forbidden" for v in result.rule_violations)
    assert result.blocked


def test_forbidden_check_gdpr_rows_from_llm_are_filtered_out():
    """GDPR остава изцяло код-ниво — ред 'gdpr', върнат по грешка от лекия LLM изход, се маха."""
    knowledge = make_brand_knowledge()
    client = _client([RuleViolation(rule="gdpr", detail="не е моя работа тук")])

    result = verifier.check_candidate(
        "post_1.md", "Текст.", briefing_text="Текст.", pool=_pool(), knowledge=knowledge, client=client
    )

    assert not any(v.rule == "gdpr" for v in result.rule_violations)


def test_check_all_writes_facts_check_json(tmp_path):
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко."
    candidates = [("post_1.md", "Пост един."), ("post_2.md", "Пост два.")]
    results = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge, _client())

    assert len(results) == 2
    assert (tmp_path / "facts_check.json").exists()


def test_check_all_calls_forbidden_check_once_per_candidate(tmp_path):
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко."
    candidates = [("post_1.md", "Пост един."), ("post_2.md", "Пост два."), ("post_3.md", "Пост три.")]
    client = _client()

    verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge, client)

    assert client.calls == 3


def test_check_all_resumable_without_force(tmp_path):
    knowledge = make_brand_knowledge()
    briefing_text = "Кратко."
    candidates = [("post_1.md", "Пост един.")]
    first = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge, _client())
    second = verifier.check_all(tmp_path, candidates, briefing_text, _pool(), knowledge, _client())

    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]
