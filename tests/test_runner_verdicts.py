"""
Интеграционни тестове за runner.run_pipeline след промяната от 2026-08-11: Критикът вече
не отхвърля постове (виж docs/technical/process-3.md). Каквото намери във Post I става
дневен ред за Post II, а Консолидаторът съставя официалното предложение от двата.

Тези тестове пазят точно това свойство — че конвейерът ВИНАГИ стига до предложение — и
пазят стария режим да не се промъкне обратно: няма присъда, която да хвърля изключение,
и няма цикъл от опити за пренаписване на един и същи текст.

Ползва FakeSequencedClient (предварително зададени отговори по роля) вместо реални HTTP
извиквания — тества само оркестрацията в runner.py, не LLMClient.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.brand.format_rules import parse_format_rules
from src.errors import ModelUnavailableError
from src.llm.roles import Role
from src.llm.schema import (
    CritiqueSchema,
    FactCheck,
    FactSource,
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

CLEAN_CRITIQUE = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")


def tone_critique(*issues: str) -> CritiqueSchema:
    return CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION, facts=[], tone_issues=list(issues), rule_violations=[], summary=""
    )


class FakeSequencedClient:
    """Връща предварително зададени отговори по роля, поред. Гърми, ако опашката за
    дадена роля се изчерпи — това доказва, че runner НЕ е викал модела повече пъти
    от очакваното (напр. не се върти в цикъл от ревизии)."""

    def __init__(self, responses: dict[Role, list[Any]]) -> None:
        self._queues = {role: list(values) for role, values in responses.items()}
        self.prompts: dict[Role, list[str]] = {}
        self.call_log: list[Any] = []

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        queue = self._queues.get(role)
        if not queue:
            raise AssertionError(f"Няма повече заредени отговори за роля {role} — извикан е повече пъти от очакваното.")
        self.prompts.setdefault(role, []).append(prompt)
        value = queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _write_briefing(post_dir):
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "briefing.md").write_text("Проведохме събитие вчера.", encoding="utf-8")


def make_client(
    *,
    writer: list[Any],
    critic: list[Any],
    consolidator: list[Any],
    media: MediaSelectionSchema | None = None,
) -> FakeSequencedClient:
    return FakeSequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: writer,
            Role.MEDIA: [media or MediaSelectionSchema(selected=[])],
            Role.CRITIC: critic,
            Role.CONSOLIDATOR: consolidator,
        }
    )


# --------------------------------------------------------------- основният нов поток


def test_findings_produce_second_post_and_consolidator_sees_both(tmp_path):
    """Сърцевината на промяната: критиката води до втори пост, а не до отказ, и
    Консолидаторът получава ДВАТА поста, не само последния."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I с клише.", "Пост II без клише."],
        critic=[tone_critique("изразът 'вълнуващо' е клише"), CLEAN_CRITIQUE],
        consolidator=["Официално предложение."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert (post_dir / "post_ii.md").read_text(encoding="utf-8").strip() == "Пост II без клише."
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Официално предложение."
    assert result.merged_from == ["post_i.md", "post_ii.md"]

    merge_prompt = client.prompts[Role.CONSOLIDATOR][0]
    assert "Пост I с клише." in merge_prompt
    assert "Пост II без клише." in merge_prompt
    assert "клише" in merge_prompt  # находката пътува до Консолидатора, за да не я върне обратно


def test_critique_findings_reach_the_second_post_prompt(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[FactCheck(claim="40 членове", source=FactSource.UNVERIFIED, note="няма в брифа")],
        tone_issues=["звучи като превод от английски"],
        rule_violations=[],
        summary="формулирай благодарността структурно, без имена",
    )
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[critique, CLEAN_CRITIQUE],
        consolidator=["Предложение."],
    )

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    second_prompt = client.prompts[Role.WRITER][1]
    assert "формулирай благодарността структурно" in second_prompt
    assert "звучи като превод от английски" in second_prompt
    assert "40 членове" in second_prompt


def test_no_findings_skips_the_second_post(tmp_path):
    """Няма какво да се запълва → втори модел ход не се харчи, консолидира се само Post I."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Чист пост."],  # само ЕДИН елемент — втори писателски ход би гръмнал
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],
        consolidator=["Чист пост."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert result.merged_from == ["post_i.md"]
    assert not (post_dir / "post_ii.md").exists()


# --------------------------------------------------------------- нищо не отхвърля поста


def test_forbidden_violation_no_longer_stops_the_pipeline(tmp_path):
    """
    До 2026-08-11 това хвърляше NeedsHumanReviewError и човекът оставаше без текст. Сега
    находката е дневен ред за втория пост, предложението излиза, а ако проблемът остане в
    него — стои като открит въпрос, не като отказ.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    forbidden = CritiqueSchema(
        verdict=Verdict.HARD_REJECT,
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="forbidden", detail="критикува конкретна фирма-конкурент")],
        summary="",
    )
    client = make_client(
        writer=["Пост I с критика на конкурент.", "Пост II без нея."],
        critic=[forbidden, CLEAN_CRITIQUE],
        consolidator=["Предложение без критика на конкурент."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert result.open_notes == []
    assert (post_dir / "output" / "post.md").exists()
    assert "критикува конкретна фирма-конкурент" in client.prompts[Role.WRITER][1]


def test_verbatim_copy_of_a_draft_becomes_an_open_note(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[tone_critique("клише"), CLEAN_CRITIQUE],
        consolidator=["Пост II."],  # Консолидаторът връща втория пост наготово
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert any("дословно" in note for note in result.open_notes)
    assert result.post_path.exists()  # предложението пак излиза — това е бележка, не спирач


def test_problem_surviving_into_the_proposal_becomes_an_open_note(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    persistent = tone_critique("все още звучи като клише")
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[persistent, persistent],  # вторият е проверката НА ПРЕДЛОЖЕНИЕТО
        consolidator=["Предложение, което още куца."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert any("клише" in note for note in result.open_notes)
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Предложение, което още куца."


def test_gdpr_finding_is_an_agenda_item_and_then_an_open_note(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    gdpr = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[],
        tone_issues=[],
        rule_violations=[RuleViolation(rule="gdpr", detail="организация без потвърдено съгласие")],
        summary="",
    )
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[gdpr, gdpr],
        consolidator=["Предложение."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert "GDPR" in client.prompts[Role.WRITER][1]
    assert any("организация без потвърдено съгласие" in note for note in result.open_notes)
    assert result.status == "ready_with_notes"


def test_no_critique_verdict_raises(tmp_path):
    """Регресионна ограда: нито една от трите присъди не бива да хвърля изключение."""
    post_dir = tmp_path / "post"
    for index, verdict in enumerate((Verdict.OK, Verdict.NEEDS_REVISION, Verdict.HARD_REJECT)):
        target = post_dir.with_name(f"post-{index}")
        _write_briefing(target)
        critique = CritiqueSchema(
            verdict=verdict,
            facts=[],
            tone_issues=["нещо" if verdict != Verdict.OK else ""] if verdict != Verdict.OK else [],
            rule_violations=(
                [RuleViolation(rule="forbidden", detail="нещо забранено")] if verdict == Verdict.HARD_REJECT else []
            ),
            summary="",
        )
        client = make_client(
            writer=["Пост I.", "Пост II."],
            critic=[critique, CLEAN_CRITIQUE],
            consolidator=["Предложение."],
        )

        result = run_pipeline(target, client, knowledge=make_brand_knowledge())

        assert result.post_path.exists()


# --------------------------------------------------------------- подсигуряване на стъпки 5–7


def test_broken_second_post_falls_back_to_post_i(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I.", ModelUnavailableError("writer", "m", 3)],
        critic=[tone_critique("клише"), CLEAN_CRITIQUE],
        consolidator=["Предложение само от Post I."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert result.merged_from == ["post_i.md"]
    assert any("Вторият пост не беше написан" in note for note in result.open_notes)
    assert result.post_path.exists()


def test_broken_consolidator_still_leaves_a_proposal(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[tone_critique("клише"), CLEAN_CRITIQUE],
        consolidator=[ModelUnavailableError("consolidator", "m", 3)],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Пост II."
    assert any("Консолидаторът не успя" in note for note in result.open_notes)


def test_broken_final_check_does_not_lose_the_proposal(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[tone_critique("клише"), ModelUnavailableError("critic", "m", 3)],
        consolidator=["Предложение."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Предложение."
    assert any("Финалната проверка не успя" in note for note in result.open_notes)


# --------------------------------------------------------------- механични правила


def test_mechanical_rules_are_applied_to_the_proposal_before_it_is_written(tmp_path):
    """
    Целият смисъл на стъпка 6б: изходът е публикуем без ръчно чистене. Хаштагът и
    измисленият домейн не бива да стигат до output/post.md, а Критикът в стъпка 7 трябва
    да чете вече поправения текст.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    knowledge = make_brand_knowledge(
        format_rules=parse_format_rules(
            "- Emoji тематични, 3–6 на пост: 🙏 благодарност, ✅ акценти\n- НЕ ползва хаштагове.\n",
            "",
            {"organization": {"website": "ictclustervarna.com"}},
        )
    )
    client = make_client(
        writer=["Пост I."],
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],
        consolidator=["12 души на среща. 📍 #ИКТ Вижте повече: https://измислен-домейн.org 🙏"],
    )

    result = run_pipeline(post_dir, client, knowledge=knowledge)

    written = (post_dir / "output" / "post.md").read_text(encoding="utf-8")
    assert "#ИКТ" not in written
    assert "📍" not in written
    assert "измислен-домейн" not in written
    assert "[линк]" in written
    assert "🙏" in written
    assert len(result.applied_fixes) == 3  # хаштаг, емоджи, адрес
    assert result.status == "ready_with_notes"

    final_check_prompt = client.prompts[Role.CRITIC][1]
    assert "#ИКТ" not in final_check_prompt  # Критикът чете поправения текст


def test_no_fixes_when_the_proposal_is_already_clean(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    knowledge = make_brand_knowledge(
        format_rules=parse_format_rules("- НЕ ползва хаштагове.\n", "", None)
    )
    client = make_client(
        writer=["Пост I."],
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],
        consolidator=["12 души на среща на 5 август."],
    )

    result = run_pipeline(post_dir, client, knowledge=knowledge)

    assert result.applied_fixes == []
    assert result.status == "ready"


# --------------------------------------------------------------- снимки


def test_possible_minor_photo_is_a_warning_not_a_stopping_condition(tmp_path):
    """От 2026-08-10: снимка с вероятно непълнолетни е предупреждение в meta.json/run.log,
    не спирач — човек преглежда снимковия избор преди публикуване във всички случаи."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    photos_dir = post_dir / "photos"
    photos_dir.mkdir()
    (photos_dir / "a.jpg").write_bytes(b"fake-jpeg-bytes")
    client = make_client(
        writer=["Готов текст."],
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],
        consolidator=["Готов текст."],
        media=MediaSelectionSchema(
            selected=[MediaSelectionItem(filename="a.jpg", reason="показва учениците", possible_minor=True)]
        ),
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert any("непълнолетни" in w for w in result.warnings)
    assert result.post_path.exists()
