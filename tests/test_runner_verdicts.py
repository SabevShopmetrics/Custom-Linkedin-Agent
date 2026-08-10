"""
Интеграционни тестове за runner.run_pipeline срещу трите присъди на Критика + GDPR
предупрежденията (снимки с вероятно непълнолетни, org без mention_allowed) — от
2026-08-10 предупрежденията НЕ спират конвейера (виж
docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md). Ползва FakeSequencedClient
(предварително зададени отговори по роля) вместо реални HTTP извиквания или CritiqueSchema
парсене — тества само оркестрацията в runner.py, не LLMClient.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.errors import NeedsHumanReviewError
from src.llm.roles import Role
from src.llm.schema import (
    CritiqueSchema,
    MediaSelectionItem,
    MediaSelectionSchema,
    PlanSchema,
    RuleViolation,
    Verdict,
)
from src.pipeline.runner import run_pipeline
from tests.conftest import make_brand_knowledge

PLAN = PlanSchema(
    post_type="практики", format="текст+снимка", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)


class FakeSequencedClient:
    """Връща предварително зададени отговори по роля, поред. Гърми, ако опашката за
    дадена роля се изчерпи — това доказва, че runner НЕ е викал модела повече пъти
    от очакваното (напр. не прави ревизия при hard_reject)."""

    def __init__(self, responses: dict[Role, list[Any]]) -> None:
        self._queues = {role: list(values) for role, values in responses.items()}
        self.call_log: list[Any] = []

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        queue = self._queues.get(role)
        if not queue:
            raise AssertionError(f"Няма повече заредени отговори за роля {role} — извикан е повече пъти от очакваното.")
        return queue.pop(0)


def _write_briefing(post_dir):
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "briefing.md").write_text("Проведохме събитие вчера.", encoding="utf-8")


def test_ok_verdict_goes_straight_to_ready(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    ok_critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="добре")
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Готов текст на пост."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [ok_critique],
            Role.CONSOLIDATOR: ["Готов текст на пост."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert (post_dir / "post_ii.md").read_text(encoding="utf-8") == "Готов текст на пост."


def test_hard_reject_stops_immediately_without_revision_attempt(tmp_path):
    """forbidden (rules/forbidden.md) си остава твърд отказ — за разлика от GDPR."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    reject_critique = CritiqueSchema(
        verdict=Verdict.HARD_REJECT,
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="forbidden", detail="критикува конкретна фирма-конкурент")],
        summary="",
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова с проблем."],  # само ЕДИН елемент — ревизия не трябва да се извика
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [reject_critique],
            Role.CONSOLIDATOR: ["Чернова с проблем."],
        }
    )

    with pytest.raises(NeedsHumanReviewError) as exc_info:
        run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert exc_info.value.immediate is True
    assert "критикува конкретна фирма-конкурент" in exc_info.value.reasons


def test_gdpr_only_hard_reject_from_llm_is_downgraded_and_does_not_stop_pipeline(tmp_path):
    """
    Регресионен тест за реален пропуск: ако LLM-ът самò реши verdict=hard_reject, но
    единствените rule_violations са GDPR (не forbidden), кодът трябва да коригира
    присъдата надолу — политиката е "GDPR е предупреждение", наложена в кода, не по
    доверие към преценката на модела (виж critic.enforce_code_level_rules).
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    confused_critique = CritiqueSchema(
        verdict=Verdict.HARD_REJECT,  # моделът греши тук — трябва да е "ok"
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="gdpr", detail="организация без потвърдено съгласие")],
        summary="",
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Готов текст."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [confused_critique],
            Role.CONSOLIDATOR: ["Готов текст."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert "организация без потвърдено съгласие" in result.warnings


def test_needs_revision_then_ok_succeeds_after_one_retry(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    needs_revision = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION, facts=[], tone_issues=["тонът е твърде небрежен"], rule_violations=[], summary=""
    )
    ok_after_revision = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="сега е добре")
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Първа чернова.", "Преработена чернова."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [needs_revision, ok_after_revision],
            Role.CONSOLIDATOR: ["Преработена чернова."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), max_revision_attempts=1)

    assert result.status == "ready"
    assert (post_dir / "post_ii.md").read_text(encoding="utf-8").strip() == "Преработена чернова."


def test_needs_revision_still_rejected_after_max_attempts_raises_non_immediate(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    needs_revision = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION, facts=[], tone_issues=["все още грешен тон"], rule_violations=[], summary=""
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Първа чернова.", "Все още неточна чернова."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [needs_revision, needs_revision],
            Role.CONSOLIDATOR: ["Все още неточна чернова."],
        }
    )

    with pytest.raises(NeedsHumanReviewError) as exc_info:
        run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), max_revision_attempts=1)

    assert exc_info.value.immediate is False
    assert any("грешен тон" in r for r in exc_info.value.reasons)


def test_default_allows_two_revision_attempts_before_giving_up(tmp_path):
    """Потребителска обратна връзка 2026-08-10: една ревизия не стигаше за легитимни
    тонови напрежения — подразбирането трябва да пробва два пъти, не веднъж."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    needs_revision = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION, facts=[], tone_issues=["все още не е наред"], rule_violations=[], summary=""
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова 1.", "Чернова 2.", "Чернова 3."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [needs_revision, needs_revision, needs_revision],
            Role.CONSOLIDATOR: ["Чернова 3."],
        }
    )

    with pytest.raises(NeedsHumanReviewError):
        run_pipeline(post_dir, client, knowledge=make_brand_knowledge())  # без изричен max_revision_attempts

    assert (post_dir / "post_ii.md").exists()
    assert (post_dir / "post_iii.md").read_text(encoding="utf-8").strip() == "Чернова 3."


def test_default_second_attempt_can_succeed_where_first_did_not(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    needs_revision = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[],
        tone_issues=[],
        rule_violations=[],
        summary="формулирай благодарността структурно, без имена",
    )
    ok_critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="сега е наред")
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова 1.", "Чернова 2 — приложена препоръка.", "не се извиква"],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [needs_revision, ok_critique],
            Role.CONSOLIDATOR: ["Чернова 2 — приложена препоръка."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert (post_dir / "post_ii.md").read_text(encoding="utf-8").strip() == "Чернова 2 — приложена препоръка."
    assert not (post_dir / "post_iii.md").exists()  # успя на 2-ри опит — 3-ти не трябва да е стигнат


def test_possible_minor_photo_is_a_warning_not_a_stopping_condition(tmp_path):
    """От 2026-08-10: снимка с вероятно непълнолетни вече е предупреждение в meta.json/
    run.log, не needs_human спирач — човек преглежда снимковия избор преди публикуване
    във всички случаи, инструментът е за вътрешен маркетинг екип, не за клиенти."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    photos_dir = post_dir / "photos"
    photos_dir.mkdir()
    (photos_dir / "a.jpg").write_bytes(b"fake-jpeg-bytes")  # media_picker се нуждае от поне 1 файл, за да не поеме no-photos пътя
    ok_critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="добре")
    flagged_selection = MediaSelectionSchema(
        selected=[MediaSelectionItem(filename="a.jpg", reason="показва учениците", possible_minor=True)]
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Готов текст."],
            Role.MEDIA: [flagged_selection],
            Role.CRITIC: [ok_critique],
            Role.CONSOLIDATOR: ["Готов текст."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert any("непълнолетни" in w for w in result.warnings)
