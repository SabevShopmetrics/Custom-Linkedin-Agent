"""Тестове за src/pipeline/judge.py — стъпка [5] на v5 (план 15): сравнение по двойки,
Съдия избира, не пише."""

from __future__ import annotations

from typing import Any

from src.llm.roles import Role
from src.llm.schema import PairwiseVerdictSchema, PlanSchema
from src.pipeline import judge
from src.pipeline.verifier import CandidateVerification, ClaimVerification
from tests.conftest import make_brand_knowledge

PLAN = PlanSchema(
    post_type="практики", format="само текст", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)


class FakeJudgeClient:
    """Връща предварително зададени победители по двойка сравнение, поред."""

    def __init__(self, winners: list[str]) -> None:
        self._winners = list(winners)
        self.calls = 0

    def complete(self, role: Role, prompt: str, *, system=None, images=None, schema=None) -> Any:
        assert role is Role.JUDGE
        winner = self._winners[self.calls]
        self.calls += 1
        return PairwiseVerdictSchema(winner=winner, summary=f"печели {winner}")


def _verification(filename: str, *, unverified: int = 0, blocked: bool = False) -> CandidateVerification:
    claims = [ClaimVerification(claim=f"твърдение{i}", verified=False) for i in range(unverified)]
    violations = []
    if blocked:
        from src.llm.schema import RuleViolation

        violations = [RuleViolation(rule="forbidden", detail="нарушение")]
    return CandidateVerification(filename=filename, claims=claims, rule_violations=violations)


def test_select_picks_the_pairwise_winner(tmp_path):
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2.")]
    verifications = [_verification("post_1.md"), _verification("post_2.md")]
    client = FakeJudgeClient(winners=["B"])  # единствената двойка: A=post_1, B=post_2

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.chosen_filename == "post_2.md"
    assert result.chosen_text == "Текст 2."
    assert client.calls == 1
    assert (tmp_path / "judgement.json").exists()


def test_select_excludes_blocked_candidates(tmp_path):
    """process-4.md: кандидат с нарушение на rules/forbidden.md се изважда от избора."""
    candidates = [("post_1.md", "Забранен текст."), ("post_2.md", "Чист текст.")]
    verifications = [_verification("post_1.md", blocked=True), _verification("post_2.md")]
    client = FakeJudgeClient(winners=[])  # не бива да се вика — само 1 допустим кандидат

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.chosen_filename == "post_2.md"
    assert result.excluded_blocked == ["post_1.md"]
    assert client.calls == 0


def test_select_falls_back_when_all_candidates_are_blocked(tmp_path):
    """Никога не оставяй човека без предложение — избира най-малко непроследими твърдения."""
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2.")]
    verifications = [
        _verification("post_1.md", unverified=3, blocked=True),
        _verification("post_2.md", unverified=1, blocked=True),
    ]
    client = FakeJudgeClient(winners=[])

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.fallback_used is True
    assert result.chosen_filename == "post_2.md"  # по-малко непроследими твърдения
    assert sorted(result.excluded_blocked) == ["post_1.md", "post_2.md"]


def test_select_tie_break_uses_unverified_count(tmp_path):
    """
    Три кандидата в кръг — post_2 бие post_1, post_1 бие post_3, post_3 бие post_2 — дават
    равенство 1-1-1 по победи. Тайбрекът трябва да предпочете най-малко непроследими
    твърдения (post_3.md тук), не първия по ред.
    """
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2."), ("post_3.md", "Текст 3.")]
    verifications = [
        _verification("post_1.md", unverified=2),
        _verification("post_2.md", unverified=1),
        _verification("post_3.md", unverified=0),
    ]
    # itertools.combinations(["post_1","post_2","post_3"], 2) ред: (1,2), (1,3), (2,3).
    client = FakeJudgeClient(winners=["B", "A", "B"])  # post_2 бие post_1; post_1 бие post_3; post_3 бие post_2

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.wins == {"post_1.md": 1, "post_2.md": 1, "post_3.md": 1}
    assert result.chosen_filename == "post_3.md"


def test_select_is_resumable_without_force(tmp_path):
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2.")]
    verifications = [_verification("post_1.md"), _verification("post_2.md")]
    client = FakeJudgeClient(winners=["A"])

    first = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)
    second = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert client.calls == 1  # вторият избор е прочетен от judgement.json, не нов LLM разход
    assert second.chosen_filename == first.chosen_filename
