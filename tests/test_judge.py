"""Тестове за src/pipeline/judge.py — стъпка [5] на v5 (план 15): сравнение по двойки,
Съдия избира, не пише."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from src.llm.roles import Role
from src.llm.schema import PairwiseVerdictSchema, PlanSchema
from src.pipeline import judge
from src.pipeline.verifier import CandidateVerification, ClaimVerification
from tests.conftest import make_brand_knowledge

PLAN = PlanSchema(
    post_type="практики", format="само текст", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)

_NAME_A_RE = re.compile(r"Версия A \(([^)]+)\)")
_NAME_B_RE = re.compile(r"Версия B \(([^)]+)\)")


class FakeJudgeClient:
    """
    Връща предварително зададен победител по ДВОЙКА кандидати, не по пореден номер на
    извикване — виж research-plan-35-critique-of-plan-34.md (Критика 4): judge.select()
    вече паралелизира сравненията (ThreadPoolExecutor), затова кой поток стигне до
    complete() първи е недетерминирано. Индексиране по self.calls (старият дизайн) би
    се чупило при паралелно изпълнение; ключуване по несортирана двойка имена, извлечена
    от самия системен промпт, е коректно независимо от реда на извикванията.

    `winners` е речник {frozenset({name_a, name_b}): "победилото_име"} — самото име на
    кандидата (напр. "post_2.md"), не буквата "A"/"B" (тя зависи от реда в конкретния
    промпт, различен detail от кой кандидат реално печели).

    `sleep_seconds` (по избор) — {frozenset({name_a, name_b}): секунди} — форсира различна
    продължителност по двойка, за да се провери, че резултатът не зависи от реда на
    ЗАВЪРШВАНЕ на паралелните извиквания (виж test_select_parallel_result_is_order_independent).
    """

    def __init__(self, winners: dict[frozenset[str], str], sleep_seconds: dict[frozenset[str], float] | None = None) -> None:
        self._winners = winners
        self._sleep_seconds = sleep_seconds or {}
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def complete(self, role: Role, prompt: str, *, system=None, images=None, schema=None) -> Any:
        assert role is Role.JUDGE
        name_a_match = _NAME_A_RE.search(system or "")
        name_b_match = _NAME_B_RE.search(system or "")
        assert name_a_match and name_b_match, "системният промпт трябва да носи имената на двата кандидата"
        name_a, name_b = name_a_match.group(1), name_b_match.group(1)
        pair = frozenset({name_a, name_b})

        delay = self._sleep_seconds.get(pair)
        if delay:
            time.sleep(delay)

        with self._lock:
            self._calls += 1

        winner_name = self._winners[pair]
        winner_letter = "A" if winner_name == name_a else "B"
        return PairwiseVerdictSchema(winner=winner_letter, summary=f"печели {winner_name}")


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
    client = FakeJudgeClient(winners={frozenset({"post_1.md", "post_2.md"}): "post_2.md"})

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.chosen_filename == "post_2.md"
    assert result.chosen_text == "Текст 2."
    assert client.calls == 1
    assert (tmp_path / "judgement.json").exists()


def test_select_excludes_blocked_candidates(tmp_path):
    """process-4.md: кандидат с нарушение на rules/forbidden.md се изважда от избора."""
    candidates = [("post_1.md", "Забранен текст."), ("post_2.md", "Чист текст.")]
    verifications = [_verification("post_1.md", blocked=True), _verification("post_2.md")]
    client = FakeJudgeClient(winners={})  # не бива да се вика — само 1 допустим кандидат

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
    client = FakeJudgeClient(winners={})

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
    client = FakeJudgeClient(
        winners={
            frozenset({"post_1.md", "post_2.md"}): "post_2.md",
            frozenset({"post_1.md", "post_3.md"}): "post_1.md",
            frozenset({"post_2.md", "post_3.md"}): "post_3.md",
        }
    )

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.wins == {"post_1.md": 1, "post_2.md": 1, "post_3.md": 1}
    assert result.chosen_filename == "post_3.md"


def test_select_is_resumable_without_force(tmp_path):
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2.")]
    verifications = [_verification("post_1.md"), _verification("post_2.md")]
    client = FakeJudgeClient(winners={frozenset({"post_1.md", "post_2.md"}): "post_1.md"})

    first = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)
    second = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert client.calls == 1  # вторият избор е прочетен от judgement.json, не нов LLM разход
    assert second.chosen_filename == first.chosen_filename


def test_select_parallel_result_is_order_independent(tmp_path):
    """
    research-plan-35-critique-of-plan-34.md, Критика 4: резултатът (wins, chosen_filename)
    трябва да е идентичен независимо от реда, в който паралелните сравнения РЕАЛНО
    завършват. Форсираме обратен ред на завършване спрямо реда на подаване (последната
    двойка от itertools.combinations приключва първа, първата — последна) и проверяваме,
    че крайният избор не зависи от това.
    """
    candidates = [("post_1.md", "Текст 1."), ("post_2.md", "Текст 2."), ("post_3.md", "Текст 3.")]
    verifications = [
        _verification("post_1.md", unverified=1),
        _verification("post_2.md", unverified=0),
        _verification("post_3.md", unverified=2),
    ]
    # itertools.combinations ред: (post_1,post_2), (post_1,post_3), (post_2,post_3).
    # Обратно забавяне: първата двойка спи най-дълго (завършва последна), последната — без сън.
    client = FakeJudgeClient(
        winners={
            frozenset({"post_1.md", "post_2.md"}): "post_2.md",
            frozenset({"post_1.md", "post_3.md"}): "post_1.md",
            frozenset({"post_2.md", "post_3.md"}): "post_2.md",
        },
        sleep_seconds={
            frozenset({"post_1.md", "post_2.md"}): 0.15,
            frozenset({"post_1.md", "post_3.md"}): 0.05,
            frozenset({"post_2.md", "post_3.md"}): 0.0,
        },
    )

    result = judge.select(tmp_path, client, make_brand_knowledge(), PLAN, candidates, verifications)

    assert result.wins == {"post_1.md": 1, "post_2.md": 2, "post_3.md": 0}
    assert result.chosen_filename == "post_2.md"
    assert client.calls == 3
