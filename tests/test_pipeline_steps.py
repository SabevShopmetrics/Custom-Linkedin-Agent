"""
Възобновяемост: стъпка, чийто изходен файл вече съществува, не бива да вика модела
отново, освен ако не е подаден --force. Ползваме StubClient, който гърми при извикване,
за да сме сигурни, че skip логиката наистина пести LLM разходка, а не просто съвпада
случайно с резултата.
"""

import json
from pathlib import Path

from src.errors import BriefingMissingError, BriefingNotFilledError
from src.llm.schema import CritiqueSchema, PlanSchema, RuleViolation, Verdict
from src.pipeline import critic, media_picker, planner, writer
from src.pipeline.runner import _is_unfilled_template, run_pipeline
from tests.conftest import StubClient, make_brand_knowledge

import pytest


def test_planner_skips_when_plan_json_exists(tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    plan_data = {
        "post_type": "практики",
        "format": "текст+снимка",
        "tone": "топло",
        "needs_photos": True,
        "max_photo_count": 2,
        "is_sensitive": False,
        "notes": "",
    }
    (post_dir / "plan.json").write_text(json.dumps(plan_data), encoding="utf-8")

    result = planner.run(post_dir, StubClient(), "", "бриф текст", force=False)

    assert result.post_type == "практики"


def test_writer_skips_when_post_i_exists(tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    (post_dir / "post_i.md").write_text("вече написан текст", encoding="utf-8")
    plan = PlanSchema(
        post_type="x", format="y", tone="z", needs_photos=False, max_photo_count=0, is_sensitive=False
    )

    result = writer.write_first_draft(post_dir, StubClient(), make_brand_knowledge(), plan, "бриф", force=False)

    assert result == "вече написан текст"


def test_media_picker_skips_llm_when_photos_folder_is_empty(tmp_path):
    post_dir = tmp_path / "post"
    (post_dir / "photos").mkdir(parents=True)
    plan = PlanSchema(
        post_type="x", format="y", tone="z", needs_photos=False, max_photo_count=3, is_sensitive=False
    )

    selection, no_photos = media_picker.run(post_dir, StubClient(), plan, "бриф", force=False)

    assert no_photos is True
    assert selection.selected == []
    assert json.loads((post_dir / "media.json").read_text(encoding="utf-8"))["selected"] == []


def test_critic_skips_when_critique_file_exists(tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="добре е")
    (post_dir / "critique.md").write_text(critic._render_critique_markdown(critique), encoding="utf-8")
    plan = PlanSchema(
        post_type="x", format="y", tone="z", needs_photos=False, max_photo_count=0, is_sensitive=False
    )

    result = critic.run(post_dir, StubClient(), make_brand_knowledge(), plan, "бриф", "чернова", force=False)

    assert result.verdict == Verdict.OK
    assert result.summary == "добре е"


def test_enforce_code_level_rules_flags_unconsented_member_mention_as_gdpr_warning_not_hard_reject():
    """
    От 2026-08-10: GDPR находки НЕ спират конвейера (инструментът е за вътрешния
    маркетинг екип, не за клиенти — човек преглежда преди публикуване). Все пак се
    добавят в rule_violations, за да се вижда в critique.md и да излязат като
    предупреждение по-нагоре в runner.py.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Zariba", "mention_allowed": None}]

    critic.enforce_code_level_rules(critique, "Благодарим на Zariba за партньорството!", "бриф без Zariba", members)

    assert critique.verdict == Verdict.OK  # НЕ hard_reject
    assert any("Zariba" in v.detail for v in critique.rule_violations)
    assert critic.gdpr_warnings(critique) == [v.detail for v in critique.rule_violations]


def test_enforce_code_level_rules_matches_short_alias_in_compound_member_name():
    """
    Регресионен тест за реален пропуск: members.yaml записва имена като
    "Зариба ЕООД (Zariba)", но пост естествено споменава само късата форма ("Zariba"),
    която НИКОГА не е точна подниза на пълния низ. Проверка само срещу целия `name` пропуска
    почти всеки реален случай — открито при реално end-to-end тестване на 2026-08-10.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Зариба ЕООД (Zariba)", "mention_allowed": None}]

    critic.enforce_code_level_rules(
        critique, "Благодарим на Zariba за отворените врати за практиката.", "бриф без Zariba", members
    )

    assert critique.verdict == Verdict.OK
    assert any("Зариба ЕООД (Zariba)" in v.detail for v in critique.rule_violations)


def test_member_name_variants_extracts_alias_and_prefix_from_parenthetical_name():
    assert critic._member_name_variants("Зариба ЕООД (Zariba)") == ["Зариба ЕООД (Zariba)", "Zariba", "Зариба ЕООД"]


def test_member_name_variants_ignores_too_short_alias():
    variants = critic._member_name_variants("СИ (АБ)")
    assert "АБ" not in variants  # под 3 символа — твърде къс, фалшив риск


def test_enforce_code_level_rules_allows_consented_member_mention():
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Zariba", "mention_allowed": True}]

    critic.enforce_code_level_rules(critique, "Благодарим на Zariba за партньорството!", "бриф", members)

    assert critique.verdict == Verdict.OK
    assert critique.rule_violations == []


def test_enforce_code_level_rules_flags_personal_name_outside_briefing_as_warning():
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(critique, "Благодарим на Иван Петров за участието.", "бриф без имена", [])

    assert critique.verdict == Verdict.OK  # НЕ hard_reject — GDPR e предупреждение
    assert any("Иван Петров" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_does_not_flag_paragraph_boundary_as_name():
    """
    Регресионен тест за реален пропуск: \\s+ в _NAME_PATTERN матчваше и през нов ред —
    последната капитализирана дума на един абзац + първата на следващия се хващаха
    погрешно като измислено "лично име" (напр. "Клъстера" + "Благодарим" на нов ред,
    открито при реално end-to-end тестване на 2026-08-10).
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    draft = "Отчетохме резултатите пред Клъстера.\n\nБлагодарим на всички участници!"

    critic.enforce_code_level_rules(critique, draft, "бриф", [])

    assert critique.rule_violations == []


def test_enforce_code_level_rules_does_not_flag_name_present_in_briefing():
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Благодарим на Иван Петров за участието.", "Лектор беше Иван Петров.", []
    )

    assert critique.verdict == Verdict.OK
    assert critique.rule_violations == []


def test_enforce_code_level_rules_preserves_llm_reported_violation():
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="forbidden", detail="критикува конкурент")],
        summary="",
    )

    critic.enforce_code_level_rules(critique, "неутрален текст", "бриф", [])

    assert critique.verdict == Verdict.HARD_REJECT
    assert len(critique.rule_violations) == 1


def test_run_pipeline_raises_briefing_missing_before_touching_the_model(tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()  # няма briefing.md изобщо

    with pytest.raises(BriefingMissingError):
        run_pipeline(post_dir, StubClient(), force=False)


def test_run_pipeline_raises_briefing_missing_for_empty_briefing(tmp_path):
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    (post_dir / "briefing.md").write_text("   \n", encoding="utf-8")

    with pytest.raises(BriefingMissingError):
        run_pipeline(post_dir, StubClient(), force=False)


def test_is_unfilled_template_true_for_exact_copy(tmp_path):
    template_path = tmp_path / "template.md"
    template_path.write_text("(попълни тук)\n", encoding="utf-8")

    assert _is_unfilled_template("(попълни тук)\n", template_path=template_path) is True
    assert _is_unfilled_template("(попълни тук)", template_path=template_path) is True  # whitespace-tolerant


def test_is_unfilled_template_false_for_edited_content(tmp_path):
    template_path = tmp_path / "template.md"
    template_path.write_text("(попълни тук)\n", encoding="utf-8")

    assert _is_unfilled_template("На 5 август проведохме реално събитие.", template_path=template_path) is False


def test_is_unfilled_template_false_when_template_missing(tmp_path):
    missing_template = tmp_path / "does-not-exist.md"

    assert _is_unfilled_template("каквото и да е", template_path=missing_template) is False


def test_run_pipeline_raises_briefing_not_filled_for_unedited_example_template(tmp_path):
    """
    Реален регресионен случай от 2026-08-10: потребител пусна `new` и веднага `generate`
    без да редактира briefing.md. Планер → Писател → Критик → Ревизия → Критик изгориха
    реални LLM извиквания, преди да стигнат до същия извод, който тук се хваща безплатно.
    """
    post_dir = tmp_path / "post"
    post_dir.mkdir()
    real_template_text = Path("posts/_example/briefing.md").read_text(encoding="utf-8")
    (post_dir / "briefing.md").write_text(real_template_text, encoding="utf-8")

    with pytest.raises(BriefingNotFilledError):
        run_pipeline(post_dir, StubClient(), force=False)
