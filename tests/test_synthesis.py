"""
Двете нови части от промяната на 2026-08-11 (docs/technical/process-3.md):

  critic.collect_findings  — критиката → дневен ред за втория пост
  critic.review_notes      — критиката → списък за човека
  consolidator.synthesize  — двата поста → официално предложение

Тук се тества всяка сама за себе си; оркестрацията им е в tests/test_runner_verdicts.py.
"""

from __future__ import annotations

import json
from typing import Any

from src.llm.schema import (
    CritiqueSchema,
    FactCheck,
    FactSource,
    MediaSelectionSchema,
    PlanSchema,
    RuleViolation,
    Verdict,
)
from src.pipeline import consolidator
from src.pipeline.critic import collect_findings, review_notes


class RecordingClient:
    """Записва (system, prompt) на всяко извикване и връща предварително зададен текст."""

    def __init__(self, response: str = "Официално предложение.") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.call_log: list[Any] = []

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        self.calls.append({"role": role, "prompt": prompt, "system": system})
        return self.response


class ExplodingClient:
    call_log: list[Any] = []

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Моделът не трябваше да бъде извикван (skip логиката е нарушена).")


PLAN = PlanSchema(
    post_type="практики", format="само текст", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)


# --------------------------------------------------------------------------- дневен ред


def test_collect_findings_puts_rule_violations_first():
    """Нарушено правило е най-тежката находка — вторият пост трябва да я види първа, за да
    не се загуби между пет тонови бележки."""
    critique = CritiqueSchema(
        verdict=Verdict.HARD_REJECT,
        facts=[],
        tone_issues=["клише"],
        rule_violations=[RuleViolation(rule="forbidden", detail="политическо изказване")],
        summary="преформулирай",
    )

    findings = collect_findings(critique)

    assert "политическо изказване" in findings[0]
    assert "НАПЪЛНО" in findings[0]
    assert "преформулирай" in findings[1]


def test_collect_findings_turns_gdpr_into_a_removal_instruction():
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="gdpr", detail="организация без потвърдено съгласие")],
        summary="",
    )

    findings = collect_findings(critique)

    assert len(findings) == 1
    assert "махни" in findings[0].lower()
    assert "организация без потвърдено съгласие" in findings[0]


def test_collect_findings_includes_only_unverified_facts():
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[
            FactCheck(claim="40 членове", source=FactSource.UNVERIFIED, note="няма в брифа"),
            FactCheck(claim="основан 2008", source=FactSource.FACTS_YAML, note=""),
        ],
        tone_issues=[],
        rule_violations=[],
        summary="",
    )

    findings = collect_findings(critique)

    assert any("40 членове" in f for f in findings)
    assert not any("2008" in f for f in findings)


def test_collect_findings_is_empty_for_a_clean_critique():
    """Празният списък е решението „пропусни втория пост" в runner.py — ако тук се промъкне
    дори един празен низ, конвейерът ще харчи модел без причина."""
    clean = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    assert collect_findings(clean) == []


def test_review_notes_are_statements_not_instructions():
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[FactCheck(claim="40 членове", source=FactSource.UNVERIFIED, note="няма в брифа")],
        tone_issues=["звучи като превод"],
        rule_violations=[RuleViolation(rule="gdpr", detail="лично име извън брифа")],
        summary="няма значение за човека",
    )

    notes = review_notes(critique)

    assert "[GDPR] лично име извън брифа" in notes
    assert "звучи като превод" in notes
    assert any("40 членове" in note for note in notes)
    assert not any("махни" in note.lower() for note in notes)  # човекът чете констатации


# --------------------------------------------------------------------------- синтез


def test_synthesize_sends_both_posts_and_findings_to_the_model(tmp_path):
    client = RecordingClient()

    text, merged_from = consolidator.synthesize(
        tmp_path,
        client,
        first_post="Версия I текст.",
        second_post="Версия II текст.",
        findings=["клишето 'вълнуващо'"],
    )

    assert text == "Официално предложение."
    assert merged_from == ["post_i.md", "post_ii.md"]
    assert (tmp_path / "output" / "post.md").read_text(encoding="utf-8").strip() == "Официално предложение."

    prompt = client.calls[0]["prompt"]
    assert "Версия I текст." in prompt
    assert "Версия II текст." in prompt
    assert "клишето 'вълнуващо'" in prompt
    assert "силата на" in client.calls[0]["system"]  # промптът за синтез, не за чистене


def test_synthesize_without_second_post_falls_back_to_cleanup(tmp_path):
    client = RecordingClient(response="Изчистена Версия I.")

    text, merged_from = consolidator.synthesize(
        tmp_path, client, first_post="Версия I.", second_post=None, findings=[]
    )

    assert merged_from == ["post_i.md"]
    assert text == "Изчистена Версия I."
    assert "Механична задача" in client.calls[0]["system"]


def test_synthesis_bypassed_detects_a_verbatim_copy():
    """Наблюдавано на 2026-08-11: Консолидаторът върна Версия II дума по дума. Изходът
    изглежда нормален, затова случаят се хваща в кода."""
    assert consolidator.synthesis_bypassed("Версия II.", "Версия I.", "Версия II.") == "post_ii.md"
    assert consolidator.synthesis_bypassed("Версия I.", "Версия I.", "Версия II.") == "post_i.md"
    assert consolidator.synthesis_bypassed("Нещо трето.", "Версия I.", "Версия II.") is None


def test_synthesis_bypassed_ignores_whitespace_differences():
    assert consolidator.synthesis_bypassed("Версия  II.\n", "I", "Версия II.") == "post_ii.md"


def test_merge_prompt_forbids_returning_one_version_unchanged():
    """Без това изискване моделът реално връщаше Версия II наготово и стъпката губеше смисъл."""
    assert "дословно" in consolidator.MERGE_SYSTEM_PROMPT
    assert "част по част" in consolidator.MERGE_SYSTEM_PROMPT


def test_synthesize_forbids_inventing_facts_in_the_merge_prompt():
    """Синтезът е избор между два текста, не писане на трети — ако тази граница падне от
    промпта, Консолидаторът става втори Писател без фактическа проверка след него."""
    assert "НЕ добавяй факти" in consolidator.MERGE_SYSTEM_PROMPT
    assert "не пишеш нов пост" in consolidator.MERGE_SYSTEM_PROMPT


def test_synthesize_skips_the_model_when_proposal_already_exists(tmp_path):
    post_path = tmp_path / "output" / "post.md"
    post_path.parent.mkdir(parents=True)
    post_path.write_text("Вече готово предложение.\n", encoding="utf-8")

    text, merged_from = consolidator.synthesize(
        tmp_path, ExplodingClient(), first_post="I", second_post="II", findings=[]
    )

    assert text == "Вече готово предложение."
    assert merged_from == ["post_i.md", "post_ii.md"]


def test_synthesize_recomputes_with_force(tmp_path):
    post_path = tmp_path / "output" / "post.md"
    post_path.parent.mkdir(parents=True)
    post_path.write_text("Старо предложение.\n", encoding="utf-8")
    client = RecordingClient(response="Ново предложение.")

    text, _ = consolidator.synthesize(
        tmp_path, client, first_post="I", second_post="II", findings=[], force=True
    )

    assert text == "Ново предложение."


# --------------------------------------------------------------------------- meta.json


def test_finalize_records_status_notes_and_sources(tmp_path):
    result = consolidator.finalize(
        tmp_path,
        RecordingClient(),
        PLAN,
        MediaSelectionSchema(selected=[]),
        no_photos=True,
        status="ready_with_notes",
        open_notes=["остава недоказано твърдение"],
        warnings=["снимка с вероятно непълнолетни"],
        merged_from=["post_i.md", "post_ii.md"],
    )

    meta = json.loads((tmp_path / "output" / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ready_with_notes"
    assert meta["open_notes"] == ["остава недоказано твърдение"]
    assert meta["warnings"] == ["снимка с вероятно непълнолетни"]
    assert meta["merged_from"] == ["post_i.md", "post_ii.md"]
    assert result.open_notes == ["остава недоказано твърдение"]
    assert result.merged_from == ["post_i.md", "post_ii.md"]


def test_finalize_copies_selected_photos_and_reports_missing(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"x")
    selection = MediaSelectionSchema.model_validate(
        {"selected": [{"filename": "a.jpg", "reason": "ok"}, {"filename": "b.jpg", "reason": "липсва"}]}
    )

    consolidator.finalize(
        tmp_path,
        RecordingClient(),
        PLAN,
        selection,
        no_photos=False,
        status="ready",
    )

    meta = json.loads((tmp_path / "output" / "meta.json").read_text(encoding="utf-8"))
    assert (tmp_path / "output" / "selected" / "a.jpg").exists()
    assert meta["photos"]["selected"] == ["a.jpg"]
    assert meta["photos"]["missing_referenced_by_media_picker"] == ["b.jpg"]
