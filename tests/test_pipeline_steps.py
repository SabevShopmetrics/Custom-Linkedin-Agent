"""
Възобновяемост: стъпка, чийто изходен файл вече съществува, не бива да вика модела
отново, освен ако не е подаден --force. Ползваме StubClient, който гърми при извикване,
за да сме сигурни, че skip логиката наистина пести LLM разходка, а не просто съвпада
случайно с резултата.
"""

import json
from pathlib import Path

from src.errors import BriefingMissingError, BriefingNotFilledError
from src.llm.schema import CritiqueSchema, FactCheck, FactSource, PlanSchema, RuleViolation, Verdict
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


def test_notes_flag_thin_brief_distinguishes_scarce_from_adequate():
    """
    Регресионен тест за реална инконсистентност между repl.py's precheck и runner.py's Б-3
    находка за дължина, открита при живо тестване (2026-09-01, виж research-plan-26): двете
    места проверяваха собствен, различаващ се regex за същия сигнал — сега споделят
    planner.notes_flag_thin_brief.
    """
    assert planner.notes_flag_thin_brief("Брифът е тънък — само 2 факта.") is True
    assert planner.notes_flag_thin_brief("Брифът е тънък, но достатъчен: 4 конкретни елемента.") is False
    assert planner.notes_flag_thin_brief("Брифът е тънък, но съдържателен – богат на детайли.") is False
    assert planner.notes_flag_thin_brief("Обичаен бриф с достатъчно детайли.") is False
    assert planner.notes_flag_thin_brief(None) is False
    assert planner.notes_flag_thin_brief("") is False


def test_planner_prompt_excludes_positive_competitor_mentions_from_topic_replacement():
    """
    Регресионен тест за реален бъг от живо тестване (2026-09-02, wf-round1-3): Планерът
    маркира requires_topic_replacement=true за чисто позитивно, некритично поздравление
    към конкурентен клъстер ("не пасва на профила на страницата"), макар forbidden.md да
    забранява само критика/сравнение с назована фирма, не похвала. Промптът трябва изрично
    да изключва "не пасва на профила" и похвала/поздравление за друга организация (дори
    конкурент) като валидни причини за това поле — иначе конвейерът грешно пропуска
    Критик/Post II и показва на човека фалшиво "НАРУШАВА ПРАВИЛАТА" съобщение за нормален,
    политики-съобразен пост.
    """
    prompt = planner.SYSTEM_PROMPT_TEMPLATE.lower()
    assert "не съвпада с обичайния профил на страницата" in prompt
    assert "не оправдава requires_topic_replacement" in prompt
    assert "похвала" in prompt and "поздравление" in prompt
    assert "конкурент" in prompt
    # изрично казва, че положително споменаване не е критика/сравнение
    assert 'не е "критика или сравнение"' in prompt


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


def test_enforce_code_level_rules_does_not_flag_institution_name_as_personal_name():
    """
    Регресионен тест за реален фалшив положителен, потвърден ДВАЖДИ при живо тестване
    (2026-08-28, 2026-08-31, виж research-plan-24-institution-gdpr-false-positive-and-word-floor.md):
    "Община Варна" съвпада перфектно с "Име Фамилия" формата на _NAME_PATTERN (две
    последователни капитализирани кирилски думи), но е институция, не лице.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Подновихме партньорството с Община Варна за следващите три години.", "бриф без общината", []
    )

    assert critique.rule_violations == []


def test_enforce_code_level_rules_does_not_flag_english_job_title_as_unknown_organization():
    """
    Регресионен тест за реален фалшив положителен от живо тестване (2026-09-02,
    posts/2026-09-02-r4-job): бриф за обява за работа съдържаше длъжност на английски
    ("Junior Data Analyst") — три последователни капитализирани латински думи, перфектно
    съвпадение с _LATIN_ORG_PATTERN (Проверка 3), но е роля, не организация.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique,
        "Зангадор търсят Junior Data Analyst на пълен работен ден.",
        "бриф за обява",
        [],
    )

    assert not any("Junior Data Analyst" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_still_flags_a_real_latin_organization_ending_in_a_job_word():
    """Огледален тест: истинска организация НЕ бива да се изключва само защото случайно
    завършва на дума, съвпадаща с job-title опашка — тук нарочно избираме организация, чиято
    последна дума НЕ е в _JOB_TITLE_TAIL_WORDS, за да потвърдим, че Проверка 3 общо взето
    още работи след добавянето на изключението."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Партнираме си с Neurolink Systems за новата инициатива.", "бриф без тази фирма", []
    )

    assert any("Neurolink Systems" in v.detail for v in critique.rule_violations)


def test_enforce_code_level_rules_flags_direct_quote_attribution_even_when_name_is_in_brief():
    """
    Регресионен тест за реален пропуск от живо тестване (2026-09-02,
    posts/2026-09-02-t5-quote): Проверка 2 изключва имена, дословно присъстващи в брифа
    ("операторът вероятно познава човека") — разумно за обичайно споменаване, но публично
    приписано ИЗКАЗВАНЕ е по-силна форма на публично обвързване, изискваща отделно съгласие
    за цитиране. Брифът съдържаше буквално "Иван Петров ми каза, че...".
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    briefing = 'Иван Петров от Зангадор ми каза, че е било "най-полезното обучение".'

    critic.enforce_code_level_rules(
        critique,
        "Иван Петров сподели, че това е било най-полезното обучение на което е присъствал.",
        briefing,
        [],
    )

    assert any("Иван Петров" in v.detail and v.rule == "gdpr" for v in critique.rule_violations)


def test_enforce_code_level_rules_flags_dash_style_testimonial_attribution():
    """
    Регресионен тест за реален пропуск, открит при ПОВТОРНО живо пускане на същия бриф
    (2026-09-02, posts/2026-09-02-t5-recheck): Писателят цитира изказването не като "Име
    сподели, че..." (единственият образец, покрит от първата версия на тази проверка), а
    като тестимониална визитка — '„...цитат..." – Име Фамилия' — реален, вероятно по-чест
    формат за LinkedIn тестимониали, който трябва да се хване по същата логика.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    briefing = 'Иван Петров от Зангадор ми каза, че е било "най-полезното обучение".'

    critic.enforce_code_level_rules(
        critique,
        '„Най-полезното обучение, на което съм присъствал." – Иван Петров',
        briefing,
        [],
    )

    assert any("Иван Петров" in v.detail and v.rule == "gdpr" for v in critique.rule_violations)


def test_enforce_code_level_rules_dash_after_org_name_is_not_flagged():
    """Огледален тест: тире след собственото име на организацията ('ИКТ Клъстер – Варна')
    не бива да се бърка с тестимониална атрибуция — липсва затваряща кавичка преди тирето."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Благодарим на партньорите на ИКТ Клъстер – Варна за подкрепата.", "бриф", []
    )

    assert critique.rule_violations == []


def test_enforce_code_level_rules_ordinary_name_mention_from_brief_is_not_flagged_by_quote_check():
    """Огледален тест: обичайно споменаване на име БЕЗ глагол на говорене в непосредствена
    близост не бива да се хваща от Проверка 5 — само истински цитат/изказване."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    briefing = "Благодарим на Иван Петров за помощта при организацията на събитието."

    critic.enforce_code_level_rules(
        critique, "Благодарим на Иван Петров за помощта при организацията на събитието.", briefing, []
    )

    assert critique.rule_violations == []


def test_enforce_code_level_rules_does_not_duplicate_when_quoted_name_is_not_in_brief():
    """Ако името НЕ е в брифа, Проверка 2 вече го хваща като 'извън брифа' — Проверка 5 не
    бива да добавя втора, дублираща GDPR бележка за същото име."""
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique,
        "Иван Петров сподели, че това е било най-полезното обучение.",
        "бриф без никакво име на служител",
        [],
    )

    gdpr_notes_about_ivan = [v for v in critique.rule_violations if "Иван Петров" in v.detail]
    assert len(gdpr_notes_about_ivan) == 1


def test_enforce_code_level_rules_does_not_flag_geographic_direction_as_personal_name():
    """
    Регресионен тест за реален фалшив положителен, потвърден ДВА пъти в едно адверсариално
    живо тестване (2026-08-31, виж research-plan-25-adversarial-live-testing-findings.md):
    "Северна Европа" и "Североизточна България" съвпадат с "Име Фамилия" формата на
    _NAME_PATTERN — същият клас проблем като институциите, но за географски посоки.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(
        critique, "Партньорството отвори врати към клиенти от Северна Европа.", "бриф без региони", []
    )
    critic.enforce_code_level_rules(
        critique, "Работим активно в Североизточна България.", "бриф без региони", []
    )

    assert critique.rule_violations == []


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


def test_enforce_code_level_rules_flags_statistic_attributed_to_fabricated_institute_as_forbidden():
    """
    Регресионен тест за реален бъг от живо тестване (2026-09-02, posts/2026-09-02-wf-round1-5,
    категория fabricated_third_party_statistic): постът цитираше "73% от МСП... според
    проучване на Института за дигитална трансформация – Варна" — институт, който не
    съществува никъде в facts.yaml/members.yaml. Проверка 1 на ФАКТИ приемаше твърдението за
    "source: briefing" (дословно в брифа) и никога не го маркираше UNVERIFIED — брифът доказва
    само какво е написал операторът, не че третата страна реално съществува. Проверка 4 хваща
    точно този клас: статистика, приписана на непозната институция, е "forbidden" (вдига
    HARD_REJECT), не само мека "gdpr" бележка.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    draft = (
        "73% от малките и средни предприятия във Варненска област вече използват инструменти "
        "за изкуствен интелект. Тези данни от проучване на Института за дигитална "
        "трансформация – Варна показват, че технологиите са част от текущата работна среда."
    )
    briefing = (
        "според Институт за дигитална трансформация - Варна, 73% от МСП в областта вече "
        "ползват AI инструменти"
    )

    critic.enforce_code_level_rules(critique, draft, briefing, [])

    assert critique.verdict == Verdict.HARD_REJECT
    forbidden = [v for v in critique.rule_violations if v.rule == "forbidden"]
    assert len(forbidden) == 1
    assert "Института за дигитална трансформация" in forbidden[0].detail


def test_enforce_code_level_rules_flags_unknown_institution_mention_without_stat_as_gdpr_only():
    """
    Огледално на предишния тест: споменаване на непозната институция БЕЗ придружаваща
    статистика/число/дума за проучване остава мека "gdpr" находка (същия клас като Проверка
    3) — не всяко неизвестно институционно име е автоматично "forbidden", само когато носи
    конкретна статистика/данни, приписани на него (виж rules/forbidden.md).
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")

    critic.enforce_code_level_rules(critique, "Благодарим на Център Ретро за партньорството!", "бриф без този център", [])

    assert critique.verdict == Verdict.OK  # само gdpr, не hard_reject
    assert any(
        v.rule == "gdpr" and "Център Ретро" in v.detail for v in critique.rule_violations
    )
    assert not any(v.rule == "forbidden" for v in critique.rule_violations)


def test_enforce_code_level_rules_does_not_flag_known_member_institute_citing_a_statistic():
    """
    Проверка 4 не бива да флагне реален член на клъстера ("Изследователски институт
    "Зангадор"", виж members.yaml), дори когато е цитиран със статистика — само НЕПОЗНати
    институции.
    """
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": 'Изследователски институт "Зангадор"', "mention_allowed": True}]
    draft = 'Според проучване на Институт "Зангадор", 40% от фирмите вече ползват AI инструменти.'

    critic.enforce_code_level_rules(critique, draft, "бриф", members)

    assert not any(v.rule == "forbidden" for v in critique.rule_violations)


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


def test_collect_findings_includes_hedged_briefing_fact():
    """
    Регресионен тест за реален бъг от живо тестване (2026-09-02,
    posts/2026-09-02-wf-round1-6): факт с source="briefing" (проследим до брифа) НЕ е
    UNVERIFIED, но брифът самият го изразява като хеджирана догадка ("струва ми се, не
    съм сигурна"). Преди FactCheck.hedged такъв факт беше невидим за collect_findings,
    защото тя филтрираше строго по `source is FactSource.UNVERIFIED` — Post II никога
    не получаваше инструкция да го смекчи или премахне.
    """
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[
            FactCheck(
                claim="три нови гимназии",
                source=FactSource.BRIEFING,
                hedged=True,
                note="изразено като предположение, не потвърдена новина",
            )
        ],
        tone_issues=[],
        rule_violations=[],
        summary="",
    )

    findings = critic.collect_findings(critique)

    assert any("три нови гимназии" in f and "Хеджирано" in f for f in findings)


def test_review_notes_includes_hedged_briefing_fact():
    """
    Огледално на предишния тест, но за пътя към ЧОВЕКА (review_notes): ако хеджираната
    догадка оцелее непроменена до финалния текст, човекът, преглеждащ "ready_with_notes",
    трябва да види предупреждение — не тишина, каквато беше преди FactCheck.hedged.
    """
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[
            FactCheck(
                claim="три нови гимназии",
                source=FactSource.BRIEFING,
                hedged=True,
                note="изразено като предположение, не потвърдена новина",
            )
        ],
        tone_issues=[],
        rule_violations=[],
        summary="",
    )

    notes = critic.review_notes(critique)

    assert any("три нови гимназии" in n and "Хеджирано" in n for n in notes)


def test_collect_findings_does_not_duplicate_unverified_fact_as_hedged():
    """Факт, който вече е UNVERIFIED, минава по UNVERIFIED клона, дори ако hedged=True би
    бил технически верен — избягва двоен ред за едно и също твърдение."""
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[FactCheck(claim="40 членове", source=FactSource.UNVERIFIED, hedged=True, note="няма в брифа")],
        tone_issues=[],
        rule_violations=[],
        summary="",
    )

    findings = critic.collect_findings(critique)

    matches = [f for f in findings if "40 членове" in f]
    assert len(matches) == 1
    assert "Недоказано твърдение" in matches[0]


def test_media_picker_prompt_keeps_all_attached_photos_but_drops_screenshots():
    # Потребителски решения 2026-10-13: всички прикачени снимки влизат (план на залата също);
    # скрийншоти от компютър — никога (реално пускане сложи терминал в поста).
    from src.pipeline import media_picker

    prompt = media_picker.SYSTEM_PROMPT_TEMPLATE.format()
    assert "ВСИЧКИ влизат в поста" in prompt
    assert "3D визуализация на залата" in prompt
    assert "скрийншоти от компютър" in prompt and "терминал" in prompt




def _review(kind="photo", strength=3, **extra):
    from src.llm.schema import PhotoKind, PhotoReviewSchema

    return PhotoReviewSchema(kind=PhotoKind(kind), description=f"{kind} {strength}", strength=strength, **extra)


def test_media_picker_assemble_drops_screen_captures_and_orders_by_strength(tmp_path):
    # 2026-10-13: една снимка = едно извикване; подредбата и изключването са правила в кода.
    from src.pipeline import media_picker

    photos = [tmp_path / n for n in ("plan.png", "people.jpg", "chat.png", "poster.png")]
    reviews = [_review("graphic", 3), _review("photo", 5), _review("screen_capture", 1), _review("graphic", 3)]
    result = media_picker._assemble(photos, reviews)
    assert [item.filename for item in result.selected] == ["people.jpg", "plan.png", "poster.png"]
    assert [item.filename for item in result.excluded] == ["chat.png"]
    assert "скрийншот" in result.excluded[0].reason


def test_media_picker_keeps_photo_whose_review_failed(tmp_path):
    from src.pipeline import media_picker

    photos = [tmp_path / n for n in ("a.jpg", "b.jpg")]
    result = media_picker._assemble(photos, [None, _review("photo", 2)])
    assert [item.filename for item in result.selected] == ["b.jpg", "a.jpg"]


def test_media_picker_drops_near_duplicate_keeping_the_stronger(tmp_path):
    from PIL import Image, ImageDraw

    from src.pipeline import media_picker

    image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(image).rectangle((50, 40, 250, 200), fill="navy")
    image.save(tmp_path / "big.png")
    image.resize((200, 150)).save(tmp_path / "small.jpg", quality=70)  # същият кадър, друг размер
    other = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(other).ellipse((150, 100, 390, 290), fill="darkred")
    other.save(tmp_path / "other.png")

    photos = [tmp_path / "small.jpg", tmp_path / "big.png", tmp_path / "other.png"]
    result = media_picker._assemble(photos, [_review("photo", 2), _review("photo", 4), _review("photo", 3)])
    assert [item.filename for item in result.selected] == ["big.png", "other.png"]
    assert result.excluded[0].filename == "small.jpg" and "big.png" in result.excluded[0].reason


def test_media_picker_caps_at_linkedin_limit(tmp_path):
    from src.pipeline import media_picker

    photos = [tmp_path / f"{i:02d}.jpg" for i in range(25)]  # несъществуващи — без хеш, без дубликати
    result = media_picker._assemble(photos, [_review() for _ in photos])
    assert len(result.selected) == media_picker.MAX_POST_PHOTOS == 20


def test_media_picker_reviews_each_photo_in_its_own_call(tmp_path):
    from src.errors import InvalidResponseError
    from src.llm.schema import PhotoReviewSchema

    post_dir = tmp_path / "post"
    (post_dir / "photos").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (post_dir / "photos" / name).write_bytes(b"x")
    answers = {"a.jpg": _review("photo", 2), "b.jpg": _review("screen_capture", 1), "c.jpg": None}
    calls = []

    class PerPhotoClient:
        def complete(self, role, prompt, *, system=None, images=None, schema=None):
            assert schema is PhotoReviewSchema and len(images) == 1
            calls.append(images[0].name)
            answer = answers[images[0].name]
            if answer is None:
                raise InvalidResponseError("media", "m", 3)
            return answer

    plan = PlanSchema(post_type="x", format="y", tone="z", needs_photos=True, max_photo_count=3, is_sensitive=False)
    selection, no_photos = media_picker.run(post_dir, PerPhotoClient(), plan, "бриф", force=False)

    assert sorted(calls) == ["a.jpg", "b.jpg", "c.jpg"]
    assert no_photos is False
    assert [item.filename for item in selection.selected] == ["a.jpg", "c.jpg"]
    assert [item.filename for item in selection.excluded] == ["b.jpg"]
