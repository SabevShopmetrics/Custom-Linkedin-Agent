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


def test_critic_receives_the_planner_notes_in_its_prompt(tmp_path):
    """
    Регресионен тест за реален пропуск, хванат при живо тестване (2026-08-26): бриф с
    невъзможна дата ("30 февруари") и невярна годишнина. Планерът правилно я разпозна в
    plan.notes ("Задължителна човешка корекция преди писане"), но тази бележка стигаше само
    до Писателя (writer.py), никога до Критика — затова невъзможната дата излезе в
    "готовия" пост без нито едно предупреждение. critic.run() вече подава plan.notes в
    системния промпт (виж _render_planner_notes_block).
    """

    class CapturingClient:
        def __init__(self) -> None:
            self.system_prompts: list[str] = []

        def complete(self, role, prompt, *, system=None, images=None, schema=None):
            self.system_prompts.append(system or "")
            return CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    post_dir = tmp_path / "post"
    post_dir.mkdir()
    plan = PlanSchema(
        post_type="x",
        format="y",
        tone="z",
        needs_photos=False,
        max_photo_count=0,
        is_sensitive=False,
        notes="КРИТИЧНО: 30 февруари не съществува. Задължителна човешка корекция преди писане.",
    )
    client = CapturingClient()

    critic.run(post_dir, client, make_brand_knowledge(), plan, "бриф", "чернова", force=False)

    assert len(client.system_prompts) == 1
    assert "30 февруари не съществува" in client.system_prompts[0]


def test_critic_principle_flags_fabricated_attached_details_as_separate_claims():
    """
    Регресионен пазач за два реални случая от 2026-08-26 (виж test_writer.py за огледалните
    поправки в Писателя): (1) име на утвърдена програма е проследимо до facts.yaml, но
    твърдение, че точно СЕГА има активност около нея, е отделно твърдение; (2) реално
    училище от members.yaml, но с измислен град ("Кюстендил") залепен до него. Критикът
    трябва да знае изрично, че всяка подробност около познато име се проверява отделно.
    """
    assert "текущ" in critic.CRITIC_PRINCIPLE.lower()
    assert "Кюстендил" in critic.CRITIC_PRINCIPLE
    assert "ОТДЕЛНО" in critic.CRITIC_PRINCIPLE


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
    assert critic._member_name_variants("Зариба ЕООД (Zariba)") == [
        "Зариба ЕООД (Zariba)", "Zariba", "Зариба", "Зариба ЕООД",
    ]


def test_member_name_variants_strips_legal_suffix_for_bare_cyrillic_brand_name():
    """
    Регресионен тест за реален пропуск, хванат при живо тестване (2026-08-26): реален пост
    естествено пише само "Зариба" (кирилица, без "ЕООД") — нито "Zariba" (латиница), нито
    "Зариба ЕООД" (изисква правната форма) съвпадат с това. Постът мина изцяло невидян за
    GDPR проверката. "Зариба" (без правна форма) вече е отделен, проверяван вариант.
    """
    variants = critic._member_name_variants("Зариба ЕООД (Zariba)")
    assert "Зариба" in variants

    variants = critic._member_name_variants("Ронева ООД (Roneva)")
    assert "Ронева" in variants


def test_enforce_code_level_rules_catches_bare_cyrillic_mention_without_legal_suffix():
    """End-to-end версия на живия пропуск: "Зариба" (без "ЕООД") в реален текст трябва да
    вдигне GDPR въпрос, не да мине невидяно."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Зариба ЕООД (Zariba)", "mention_allowed": None}]

    critic.enforce_code_level_rules(
        critique, "Събитието бе организирано в сътрудничество с компанията Зариба.", "бриф без Зариба", members
    )

    assert any("Зариба" in v.detail for v in critique.rule_violations)


def test_member_name_variants_ignores_too_short_alias():
    variants = critic._member_name_variants("СИ (АБ)")
    assert "АБ" not in variants  # под 3 символа — твърде къс, фалшив риск


def test_member_name_variants_excludes_short_institutional_prefix_before_quoted_name():
    """
    Регресионен тест за реален фалшив положителен, хванат при живо тестване на пипелайна
    (2026-08-26, PIPELINE_CHAIN=v5): "ОбУ" (members.yaml: 'ОбУ „Христо Ботев"') съвпадаше
    като подниза с началото на съвсем обикновената дума "обучение" във всеки пост за
    уъркшоп — лепеше GDPR въпрос на текст, който изобщо не споменава училището. Пълното име
    и цитираният псевдоним ("Христо Ботев") остават валидни варианти.
    """
    variants = critic._member_name_variants('ОбУ „Христо Ботев"')
    assert "ОбУ" not in variants
    assert "Христо Ботев" in variants


def test_enforce_code_level_rules_does_not_false_positive_on_institutional_prefix_substring():
    """Същият случай, на ниво enforce_code_level_rules — "обучение" не бива да буди GDPR въпрос."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": 'ОбУ „Христо Ботев"', "mention_allowed": None}]

    critic.enforce_code_level_rules(
        critique, "Очакваме 30 студенти на нашето безплатно обучение по киберсигурност.", "бриф", members
    )

    assert critique.rule_violations == []


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


def test_enforce_code_level_rules_flags_unknown_latin_organization_outside_briefing():
    """
    Проверка 3 (виж research-plan-18-critique-of-plan-17.md, А1): организация, спомената в
    текста, извън и members.yaml, и брифа — реален пример от живото adversarial тестване
    (План 17, А1), който преди мина изцяло без предупреждение.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Партньор на събитието бе Marina Tech Solutions.", "бриф без този партньор", []
    )

    assert critique.verdict == Verdict.OK  # GDPR e предупреждение, не hard_reject
    assert any("Marina Tech Solutions" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_flags_unknown_organization_with_legal_suffix():
    """Вторият реален пример от План 17, А1 — латиница + правна форма."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Благодарим на Neurolink Systems GmbH за подкрепата.", "бриф без този партньор", []
    )

    assert any("Neurolink Systems GmbH" in v.detail for v in critique.rule_violations)
    # _LATIN_ORG_PATTERN и _ORG_SUFFIX_PATTERN и двата биха хванали този низ — без дедупликация
    # излиза дублирана находка за едно и също нещо.
    assert len(critique.rule_violations) == 1


def test_enforce_code_level_rules_does_not_double_flag_known_member_as_unknown_org():
    """Проверка 3 не бива да дублира Проверка 1 за организация, вече позната от members.yaml."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Marina Tech Solutions", "mention_allowed": True}]

    critic.enforce_code_level_rules(
        critique, "Партньор на събитието бе Marina Tech Solutions.", "бриф без този партньор", members
    )

    assert critique.rule_violations == []


def test_enforce_code_level_rules_flags_unknown_organization_even_when_it_is_in_the_briefing():
    """
    Регресионен тест за реален бъг, хванат при живо тестване на 2026-08-27 (виж
    research-plan-23-live-testing-of-phase1-fixes.md): по-ранна версия на Проверка 3
    изключваше кандидат, дословно присъстващ в брифа — огледално на Проверка 2 за лични
    имена. Но и двата реални примера от План 17, мотивирали Проверка 3 (Marina Tech
    Solutions, Neurolink Systems GmbH), идваха ДИРЕКТНО от брифа — това изключение мълчаливо
    пропускаше точно случая, заради който изобщо съществува проверката. Организация, за
    която трябва да се провери СЪГЛАСИЕ, не се освобождава от проверка само защото човек я е
    написал в брифа (за разлика от лично име, виж бележката в кода).
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique,
        "Партньор на събитието бе Marina Tech Solutions.",
        "Партньор ще бъде Marina Tech Solutions.",
        [],
    )

    assert any("Marina Tech Solutions" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_does_not_flag_pure_cyrillic_region_name_as_organization():
    """
    Регресионна ограда, огледална на реален фалшив положителен на Проверка 2 (открит при
    grep на историческия posts/ архив, План 18): "Северноизточна България" (регион, не
    организация) падна в двукирилски-думи модела. Проверка 3 нарочно НЕ ползва същия модел
    за чисто кирилски низове без правна форма — виж бележката над _LATIN_ORG_PATTERN.

    Проверка 2 (лични имена, непроменена от тази промяна) си остава отделен, вече известен
    риск за точно този тип низ — тестът тук изолира само Проверка 3, не твърди, че Проверка 2
    е перфектна.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(critique, "Работим активно в Централна България.", "бриф", [])

    assert not any("непозната организация" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_does_not_suppress_unrelated_org_sharing_words_with_a_member():
    """
    Регресионен тест за риск, открит при преглед на собствения фикс за А-1 (виж
    research-plan-19-critique-of-phase1-implementation.md, Част А след А-1): по-ранна версия
    на изключващата проверка ползваше "candidate е подниза на познато име", което би потиснало
    различна, реално непозната организация само защото споделя дума с познат член.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Marina Tech Solutions", "mention_allowed": True}]

    # "Tech Solutions" е ПОДНИЗА на познатия член "Marina Tech Solutions" — точно случаят,
    # в който подниза-базирана изключваща проверка би замълчала фалшиво.
    critic.enforce_code_level_rules(
        critique,
        "Partner of the event was Tech Solutions.",
        "бриф без този партньор",
        members,
    )

    assert any("Tech Solutions" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_does_not_flag_own_organization_english_name():
    """
    Регресионен тест за реален пропуск в самата Фаза 1 (виж research-plan-19-critique-of-
    phase1-implementation.md, А-1): facts.yaml пази name_en="ICT Cluster - Varna" — Проверка 3
    иначе би флагнала собственото име на клъстера на латиница ("ICT Cluster", преди тирето)
    като "непозната организация", ако own_org_names не се подаде/провери.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique,
        "The workshop was organized by ICT Cluster - Varna for local students.",
        "бриф без английско име",
        [],
        ["Сдружение „ИКТ-Клъстер-Варна\"", "ICT Cluster - Varna"],
    )

    assert not any("непозната организация" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_still_flags_unrelated_latin_org_when_own_name_given():
    """own_org_names не бива да потиска други, реално непознати организации — само своите."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique,
        "Partner of the event was Marina Tech Solutions.",
        "бриф без този партньор",
        [],
        ["ICT Cluster - Varna"],
    )

    assert any("Marina Tech Solutions" in v.detail for v in critique.rule_violations)


def test_critic_run_wires_facts_yaml_organization_name_into_own_org_check(tmp_path):
    """
    Wiring-ниво на предишния тест: critic.run() трябва наистина да извади organization.name_en
    от knowledge.facts и да го подаде на enforce_code_level_rules — не само функцията на голо
    да поддържа параметъра.
    """

    class CapturingClient:
        def complete(self, role, prompt, *, system=None, images=None, schema=None):
            return CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    post_dir = tmp_path / "post"
    post_dir.mkdir()
    plan = PlanSchema(
        post_type="x", format="y", tone="z", needs_photos=False, max_photo_count=0, is_sensitive=False
    )
    knowledge = make_brand_knowledge(
        facts={"organization": {"name_bg": "Сдружение „ИКТ-Клъстер-Варна\"", "name_en": "ICT Cluster - Varna"}},
        facts_available=True,
    )

    critique = critic.run(
        post_dir,
        CapturingClient(),
        knowledge,
        plan,
        "бриф",
        "The event was organized by ICT Cluster - Varna.",
        force=False,
    )

    assert not any("непозната организация" in v.detail for v in critique.rule_violations)


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
