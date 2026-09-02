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

import json
import threading
import time
from datetime import date
from typing import Any

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


def _wait_for_async_final_check(post_dir, *, timeout: float = 5.0) -> dict:
    """
    Стъпка [7] на v3 стана асинхронна 2026-08-25 (виж runner.py::_start_async_final_check,
    потребителска заявка да не блокира резултата ~70-350+ секунди за чисто информативна
    стъпка). `result.open_notes`/`result.status`, върнати СИНХРОННО от run_pipeline(), вече
    НЕ съдържат находките от тази стъпка — те се дописват в output/meta.json от фонов thread.
    Тестове, които проверяват точно тези находки, трябва да изчакат нишката (по име, зададено
    в _start_async_final_check) и после да прочетат meta.json — оттук е реалният извор.
    """
    thread_name = f"final-check-{post_dir.name}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        threads = [t for t in threading.enumerate() if t.name == thread_name]
        if not threads:
            break
        for t in threads:
            t.join(timeout=0.05)
    return json.loads((post_dir / "output" / "meta.json").read_text(encoding="utf-8"))

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

def test_hedged_briefing_fact_reaches_the_second_post_prompt(tmp_path):
    """
    Регресионен тест за реален бъг от живо тестване (2026-09-02,
    posts/2026-09-02-wf-round1-6, категория long_rambling_multi_topic): Критикът
    правилно разпозна, че "три нови гимназии" е хеджирана догадка на оператора
    ("струва ми се", "не съм сигурна"), но класифицира source="briefing" (правилно —
    твърдението наистина Е в брифа). collect_findings/review_notes филтрираха строго по
    `source is FactSource.UNVERIFIED`, затова находката никога не стигаше до Post II и
    финалният пост публикуваше бройката като плосък, неквалифициран факт. FactCheck.hedged
    затваря тази дупка — трябва да пътува до Писателя независимо от `source`.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    critique = CritiqueSchema(
        verdict=Verdict.NEEDS_REVISION,
        facts=[
            FactCheck(
                claim="три нови гимназии",
                source=FactSource.BRIEFING,
                hedged=True,
                note="Фактът е в брифа, но изразен като предположение ('струва ми се'), не като потвърдена новина.",
            )
        ],
        tone_issues=[],
        rule_violations=[],
        summary="",
    )
    client = make_client(
        writer=["Пост I с три нови гимназии.", "Пост II без плоско твърдение."],
        critic=[critique, CLEAN_CRITIQUE],
        consolidator=["Предложение."],
    )

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    second_prompt = client.prompts[Role.WRITER][1]
    assert "Хеджирано твърдение от брифа" in second_prompt
    assert "три нови гимназии" in second_prompt


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

def test_v3_meta_json_includes_ai_act_note(tmp_path):
    """
    До 2026-08-25 бележката за EU AI Act чл. 50 се пишеше само в v5 — v3 (веригата по
    подразбиране) нямаше НИКАКВА следа от нея в meta.json (виж runner.py::_ai_act_meta).
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(writer=["Пост."], critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE], consolidator=["Пост."])

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    meta = json.loads((post_dir / "output" / "meta.json").read_text(encoding="utf-8"))

    assert "ai_act" in meta
    assert "чл. 50" in meta["ai_act"]["note"]


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
    meta = _wait_for_async_final_check(post_dir)

    assert meta["status"] == "ready_with_notes"
    assert any("клише" in note for note in meta["open_notes"])
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
    meta = _wait_for_async_final_check(post_dir)

    assert "GDPR" in client.prompts[Role.WRITER][1]
    assert any("организация без потвърдено съгласие" in note for note in meta["open_notes"])
    assert meta["status"] == "ready_with_notes"

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
    _wait_for_async_final_check(post_dir)

    # Асинхронна грешка (виж _start_async_final_check) не стига до result.open_notes —
    # само self-contained лог в run.log на фоновия thread; предложението пак оцелява.
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Предложение."
    assert "Финалната проверка (async) се счупи" in (post_dir / "run.log").read_text(encoding="utf-8")

def test_broken_final_check_with_non_agent_error_records_failed_status(tmp_path):
    """
    A5 (виж research-plan-18-critique-of-plan-17.md): преди тук минаваше суров traceback в
    конзолата и стъпка [7] оставаше без никаква следа в meta.json — разликата между "проверих,
    чисто е" и "не успях да проверя" изчезваше напълно. Сега широкият catch хваща и
    не-AgentError изключения (напр. AttributeError от развален JSON парсинг) и записва явен
    статус.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I.", "Пост II."],
        critic=[tone_critique("клише"), AttributeError("'NoneType' object has no attribute 'strip'")],
        consolidator=["Предложение."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    meta = _wait_for_async_final_check(post_dir)

    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Предложение."
    assert meta["final_check"]["status"] == "failed"
    assert "AttributeError" in meta["final_check"]["error"]
    assert "Traceback" not in (post_dir / "run.log").read_text(encoding="utf-8")

def test_successful_final_check_records_ok_status(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(writer=["Пост."], critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE], consolidator=["Пост."])

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    meta = _wait_for_async_final_check(post_dir)

    assert meta["final_check"] == {"status": "ok"}

# --------------------------------------------------------------- wait_for_final_check_seconds (план 29)

class _DelayedRoleClient:
    """Обвивка около FakeSequencedClient, добавяща изкуствено закъснение на N-тото извикване
    на дадена роля — единственият начин да се симулира детерминистично "стъпка [7] не успя
    в срока" (реалният `_start_async_final_check` тръгва в separate thread, отговорът идва от
    HTTP извикване с реална латентност; тук нямаме нищо да закъснее, освен да го инжектираме)."""

    def __init__(self, inner, *, delay_role, delay_call_index, delay_seconds):
        self._inner = inner
        self._delay_role = delay_role
        self._delay_call_index = delay_call_index
        self._delay_seconds = delay_seconds
        self._call_counts: dict[Role, int] = {}
        self.prompts = inner.prompts  # споделен обект — inner.complete го мутира на място
        self.call_log = inner.call_log

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        count = self._call_counts.get(role, 0)
        self._call_counts[role] = count + 1
        if role == self._delay_role and count == self._delay_call_index:
            time.sleep(self._delay_seconds)
        return self._inner.complete(role, prompt, system=system, images=images, schema=schema)

def test_wait_for_final_check_seconds_returns_async_notes_directly(tmp_path):
    """
    Реален бъг от живо тестване (2026-09-02, research-plan-29): `generate` е еднократно CLI
    извикване — процесът приключваше веднага след печат на резюмето и убиваше daemon нишката
    на стъпка [7], преди тя изобщо да напише резултат. `meta.json` показваше
    `"final_check": {"status": "pending"}` буквално ВИНАГИ, никога "ok" — не просто "невидяно
    на екрана" (познатият, приет компромис), а изобщо недовършено. С `wait_for_final_check_seconds
    > 0`, `run_pipeline()` изчаква стъпка [7] и връща находките ѝ директно в `result`, БЕЗ да е
    нужно extra polling на meta.json (за разлика от всички други тестове тук).
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост."],
        critic=[CLEAN_CRITIQUE, tone_critique("клише в предложението")],
        consolidator=["Пост."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=5.0)

    assert result.status == "ready_with_notes"
    assert any("клише в предложението" in note for note in result.open_notes)

def test_wait_for_final_check_seconds_zero_leaves_result_unchanged(tmp_path):
    """Огледален тест: подразбиращото се поведение (0, REPL/chat) НЕ включва находките на
    стъпка [7] в синхронно върнатия резултат — само в meta.json, след като фоновата нишка
    приключи. Пази старото, документирано поведение непроменено за REPL/chat пътя."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост."],
        critic=[CLEAN_CRITIQUE, tone_critique("клише в предложението")],
        consolidator=["Пост."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready"
    assert not any("клише в предложението" in note for note in result.open_notes)
    meta = _wait_for_async_final_check(post_dir)
    assert any("клише в предложението" in note for note in meta["open_notes"])

def test_wait_for_final_check_seconds_timeout_falls_back_to_pending_result(tmp_path):
    """Ако стъпка [7] не приключи в зададения срок, `run_pipeline()` връща резултата БЕЗ да
    увисне за неопределено време — best-effort, не гаранция (виж докстринга на run_pipeline)."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    inner = make_client(
        writer=["Пост."],
        critic=[CLEAN_CRITIQUE, tone_critique("находка, пристигнала твърде късно")],
        consolidator=["Пост."],
    )
    client = _DelayedRoleClient(inner, delay_role=Role.CRITIC, delay_call_index=1, delay_seconds=0.3)

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=0.05)

    assert result.status == "ready"
    assert not any("находка, пристигнала твърде късно" in note for note in result.open_notes)
    meta = _wait_for_async_final_check(post_dir, timeout=5.0)  # почисти фоновата нишка преди следващия тест
    assert any("находка, пристигнала твърде късно" in note for note in meta["open_notes"])

# --------------------------------------------------------------- недостатъчен бриф (А2/B4)

def test_insufficient_brief_skips_critic_and_second_post(tmp_path):
    """
    А2 (виж research-plan-18-critique-of-plan-17.md): когато Планерът маркира брифа като
    недостатъчен, Критик/Post II/LLM-синтез в Консолидатора НЕ се извикват изобщо — нито
    синхронно, нито асинхронно за стъпка [7] (виж research-plan-19-critique-of-phase1-
    implementation.md, А-2/А-3: асинхронната проверка тук нямаше стойност в живия тест на
    План 17 и създаваше риск от състезание с B4's автоматично регенериране). Критикът НЕ се
    извиква изобщо — опашката му остава празна, за да гръмне ако това се промени случайно.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    insufficient_plan = PlanSchema(
        post_type="общо",
        format="само текст",
        tone="неутрален",
        needs_photos=False,
        max_photo_count=0,
        is_sensitive=False,
        insufficient_brief=True,
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [insufficient_plan],
            Role.WRITER: ["Брифът за събитието не е предоставен."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [],  # НЕ бива да се извика — нито синхронно, нито асинхронно
            Role.CONSOLIDATOR: ["Брифът за събитието не е предоставен."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    meta = _wait_for_async_final_check(post_dir)

    assert result.merged_from == ["post_i.md"]
    assert result.insufficient_brief is True
    assert any("без достатъчно съдържание" in note for note in result.open_notes)
    assert not (post_dir / "post_ii.md").exists()
    assert meta["final_check"] == {"status": "skipped"}
    assert meta["insufficient_brief"] is True

def test_requires_topic_replacement_skips_critic_and_second_post(tmp_path):
    """
    Регресионен тест за реална находка от адверсариално живо тестване (2026-09-01, виж
    research-plan-26-forbidden-topics-and-numeric-relationships.md, Част А): без този байпас
    Критикът третираше честния отказ на Писателя за забранена тема (напр. религиозен повод)
    като "проблем с формата" и настояваше за "нова версия със заменена инициатива" — Post II
    отговори, като си измисли ЦЯЛО фиктивно събитие (дата + дневен ред), само за да прилича
    на нормален пост. Огледален тест на test_insufficient_brief_skips_critic_and_second_post.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    forbidden_plan = PlanSchema(
        post_type="религиозен повод", format="само текст", tone="неутрален", needs_photos=False,
        max_photo_count=0, is_sensitive=True, requires_topic_replacement=True,
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [forbidden_plan],
            Role.WRITER: ["Няма достатъчно информация — темата нарушава правилата на клъстера."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [],  # НЕ бива да се извика — нито синхронно, нито асинхронно
            Role.CONSOLIDATOR: ["Няма достатъчно информация — темата нарушава правилата на клъстера."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    meta = _wait_for_async_final_check(post_dir)

    assert result.merged_from == ["post_i.md"]
    assert result.insufficient_brief is True  # преизползва same REPL retry път
    assert any("НАРУШАВА ПРАВИЛАТА" in note for note in result.open_notes)
    assert not (post_dir / "post_ii.md").exists()
    assert meta["final_check"] == {"status": "skipped"}


def test_sufficient_brief_is_unaffected_by_the_insufficient_brief_gate(tmp_path):
    """Регресионна ограда: нормален бриф (insufficient_brief по подразбиране False) минава по стария път, непроменено."""
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Пост I с клише.", "Пост II без клише."],
        critic=[tone_critique("изразът 'вълнуващо' е клише"), CLEAN_CRITIQUE],
        consolidator=["Официално предложение."],
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.insufficient_brief is False
    assert result.merged_from == ["post_i.md", "post_ii.md"]

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
        consolidator=[
            "12 души на среща. 🙏 Благодарим на всички за участието. "
            "📍 #ИКТ Вижте повече: https://измислен-домейн.org"
        ],
    )

    result = run_pipeline(post_dir, client, knowledge=knowledge)

    written = (post_dir / "output" / "post.md").read_text(encoding="utf-8")
    assert "#ИКТ" not in written
    assert "📍" not in written
    assert "измислен-домейн" not in written
    # Видим "[линк]" в готов пост изглежда като недовършен текст (потребителска обратна
    # връзка 2026-08-17) — цялото затварящо изречение се маха, не само адресът.
    assert "[линк]" not in written
    assert "Вижте повече" not in written
    assert "🙏" in written
    assert len(result.applied_fixes) == 4  # хаштаг, емоджи, адрес, изречение с placeholder
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


# --------------------------------------------------------------- дължина под минимума (2026-08-17)


def test_short_post_i_gets_a_length_finding_and_triggers_second_post(tmp_path):
    """
    Потребителска инструкция 2026-08-17: минимумът от корпуса трябва реално да стигне до
    Писателя, не само да излезе като бележка накрая (formatting.enforce, СЛЕД
    Консолидатора — твърде късно за поправка). Проверката е код-ниво в runner.py, не LLM
    преценка на Критика.
    """
    from src.brand.format_rules import FormatRules

    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(
        writer=["Кратко.", "Много по-дълъг втори пост с конкретни факти и числа за събитието."],
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],  # Критикът сам не намира нищо — само дължината е проблем
        consolidator=["Много по-дълъг втори пост с конкретни факти и числа за събитието."],
    )
    knowledge = make_brand_knowledge(format_rules=FormatRules(words_min=20))

    result = run_pipeline(post_dir, client, knowledge=knowledge)

    # Post II беше извикан (втори елемент в опашката на writer се консумира) — доказва, че
    # findings не е бил празен, въпреки че Критикът сам по себе си върна CLEAN_CRITIQUE.
    assert len(client.prompts[Role.WRITER]) == 2
    assert "по-дълъг" in result.post_path.read_text(encoding="utf-8")

def test_length_finding_permits_honest_shortness_when_planner_flagged_a_thin_brief(tmp_path):
    """
    Регресионен тест за реална находка от адверсариално живо тестване (2026-08-31, виж
    research-plan-25, Б-3): преди фикса тази находка безусловно искаше "добави още факти",
    дори когато Планерът вече е отбелязал тънък бриф — противоречейки на "спри честно"
    правилото от tone.md и карайки Post II да запълва дупката с генеричен пълнеж.
    """
    from src.brand.format_rules import FormatRules

    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    thin_plan = PlanSchema(
        post_type="събитие", format="само текст", tone="топло", needs_photos=False,
        max_photo_count=0, is_sensitive=False, notes="Брифът е тънък — само 2 факта.",
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [thin_plan],
            Role.WRITER: ["Кратко.", "Все още кратко, но честно."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN_CRITIQUE, CLEAN_CRITIQUE],
            Role.CONSOLIDATOR: ["Все още кратко, но честно."],
        }
    )
    knowledge = make_brand_knowledge(format_rules=FormatRules(words_min=20))

    run_pipeline(post_dir, client, knowledge=knowledge)

    second_prompt = client.prompts[Role.WRITER][1]
    assert "спри честно" in second_prompt.lower() or "остави поста така" in second_prompt.lower()
    assert "добави още конкретни факти" not in second_prompt.lower()


def test_length_finding_permits_honest_shortness_for_sensitive_plan_even_without_the_word_thin(tmp_path):
    """
    Регресионен тест за реална, по-сериозна находка от адверсариално живо тестване
    (2026-09-01, виж research-plan-27): бриф за уволнение на конкретен, назован служител.
    Планерът написа бележка изцяло за чувствителност/тон (HR/юридически контрол), БЕЗ
    думата "тънък" — planner.notes_flag_thin_brief() затова връщаше False и находката
    поиска "добави факти", което накара Писателя да си измисли номер на заповед и
    позоваване на Трудовия кодекс за реално лице. plan.is_sensitive сега печели безусловно.
    """
    from src.brand.format_rules import FormatRules

    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    sensitive_plan = PlanSchema(
        post_type="кадрово решение", format="само текст", tone="сух", needs_photos=False,
        max_photo_count=0, is_sensitive=True,
        notes="Поводът изисква задължителен човешки контрол (HR/Юридически отдел) за съответствие с GDPR.",
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [sensitive_plan],
            Role.WRITER: ["Кратко.", "Все още кратко, но честно."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN_CRITIQUE, CLEAN_CRITIQUE],
            Role.CONSOLIDATOR: ["Все още кратко, но честно."],
        }
    )
    knowledge = make_brand_knowledge(format_rules=FormatRules(words_min=20))

    run_pipeline(post_dir, client, knowledge=knowledge)

    second_prompt = client.prompts[Role.WRITER][1]
    assert "спри честно" in second_prompt.lower() or "остави поста така" in second_prompt.lower()
    assert "добави още конкретни факти" not in second_prompt.lower()


def test_sensitive_plan_leaves_a_persistent_open_note(tmp_path):
    """
    Регресионен тест за реална находка от адверсариално живо тестване (2026-08-31, виж
    research-plan-25-adversarial-live-testing-findings.md, Б-1): Планерът класифицираше
    `is_sensitive: true` коректно, но това никога не стигаше до `PipelineResult.open_notes`/
    `meta.json` — виждаше се само като живо лог съобщение, изчезващо след пускането.
    CLAUDE.md изисква "чувствителните [постове] с одобрение" — липсата на персистентен
    маркер означаваше, че чувствителен пост изглежда идентично на рутинен в записания резултат.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    sensitive_plan = PlanSchema(
        post_type="съкращения", format="само текст", tone="сериозен", needs_photos=False,
        max_photo_count=0, is_sensitive=True,
    )
    client = FakeSequencedClient(
        {
            Role.PLANNER: [sensitive_plan],
            Role.WRITER: ["Пост I."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN_CRITIQUE, CLEAN_CRITIQUE],
            Role.CONSOLIDATOR: ["Официално предложение."],
        }
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert any("ЧУВСТВИТЕЛНА" in note for note in result.open_notes)

def test_anniversary_mismatch_becomes_an_open_note(tmp_path):
    """
    Регресионен тест за реална находка от адверсариално живо тестване (2026-08-31, виж
    research-plan-25, Б-2): невярна годишнина (клъстерът основан 2008 г., 2026 г. = 18, не
    5) премина непроменена през целия конвейер и излезе в публикувания текст без бележка.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    knowledge = make_brand_knowledge(facts={"organization": {"founded": 2008}})
    client = make_client(
        writer=["Пост I."],
        critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE],
        consolidator=["Основан през 2008 г., тази година клъстерът чества 5-годишнина."],
    )

    result = run_pipeline(post_dir, client, knowledge=knowledge, today=date(2026, 8, 31))

    assert any("5-годишнина" in note and "18-годишнина" in note for note in result.open_notes)
