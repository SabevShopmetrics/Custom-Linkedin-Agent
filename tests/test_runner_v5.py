"""
Интеграционни тестове за run_pipeline() по v5 (PIPELINE_CHAIN=v5) — план 15, надгражда
docs/technical/process-4.md. Огледални на tests/test_runner_verdicts.py (v3), но за новата
верига: факт-регистър → N кандидата паралелно → филтър за проследимост (код + лека LLM
проверка на rules/forbidden.md на кандидат) → съдия (pairwise) → форматиране → финална
проверка асинхронно.

Пропускът "rules/forbidden.md не се хваща ПРЕДИ избора на Съдията" (документиран по-рано в
src/pipeline/verifier.py) е затворен на 2026-08-25 — виж critic.check_forbidden_rules() и
test_v5_chain_excludes_a_forbidden_candidate_before_judge_selects по-долу.
"""

from __future__ import annotations

import time
from typing import Any

from src.llm.roles import Role
from src.llm.schema import (
    CritiqueSchema,
    ForbiddenCheckSchema,
    MediaSelectionSchema,
    PairwiseVerdictSchema,
    PlanSchema,
    RuleViolation,
    Verdict,
)
from src.pipeline.runner import run_pipeline
from src.pipeline.writer import CANDIDATE_ANGLES
from tests.conftest import make_brand_knowledge, make_config

PLAN = PlanSchema(
    post_type="практики", format="само текст", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)


class FakeV5Client:
    """
    Аналог на FakeSequencedClient (test_runner_verdicts.py), но с публичен `.config`
    (v5 разклонението в run_pipeline го чете, виж runner.py) и опашка за Role.JUDGE.
    Role.CRITIC се вика за ДВЕ различни неща в v5 (различени по `schema`, не по роля):
    лекият pre-selection forbidden-check на всеки кандидат (schema=ForbiddenCheckSchema,
    стъпка [4]) и пълната async финална проверка на избрания текст (schema=CritiqueSchema,
    стъпка [7]) — виж докстринга на critic.check_forbidden_rules за защо не е нова роля.

    ВАЖНО: `writer.write_candidates` пуска кандидатите ПАРАЛЕЛНО (ThreadPoolExecutor) —
    проста FIFO опашка би дала недетерминиран резултат (кой thread стигне пръв до
    complete()). Затова тук отговорът за Role.WRITER се избира по СТАБИЛЕН маркер в
    системния промпт — редът "ъгъл", който CANDIDATE_ANGLES[i] лепи на слот i, фиксиран
    при подаването на задачата, не при изпълнението ѝ (виж writer.py). Форбидън-проверката
    (стъпка [4]) е паралелна по същата причина — отговорът за нея се избира по стабилен
    маркер в USER промпта (самия текст на кандидата), не по ред на пристигане.
    """

    def __init__(
        self, *, writer, judge_winners, config, critic_response=None, forbidden_by_text=None
    ) -> None:
        self._writer_by_angle_index = list(writer)  # index i → текст за post_{i+1}.md
        self._judge_queue = list(judge_winners)
        self._critic_response = critic_response or CritiqueSchema(verdict=Verdict.OK)
        # {текст на кандидата: [RuleViolation, ...]} — по подразбиране "чисто" за всеки.
        self._forbidden_by_text = forbidden_by_text or {}
        self.config = config
        self.prompts: dict[Role, list[str]] = {}
        self.call_log: list[Any] = []

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        self.prompts.setdefault(role, []).append(prompt)
        if role is Role.PLANNER:
            return PLAN
        if role is Role.WRITER:
            for index, angle in enumerate(CANDIDATE_ANGLES):
                if system and angle in system:
                    return self._writer_by_angle_index[index]
            raise AssertionError("Системният промпт на кандидата не съдържа познат ъгъл.")
        if role is Role.MEDIA:
            return MediaSelectionSchema(selected=[])
        if role is Role.JUDGE:
            winner = self._judge_queue.pop(0)
            return PairwiseVerdictSchema(winner=winner, summary=f"печели {winner}")
        if role is Role.CRITIC and schema is ForbiddenCheckSchema:
            # Стъпка [4], pre-selection — виж critic.check_forbidden_rules. `prompt` тук е
            # директно текстът на кандидата (client.complete(Role.CRITIC, text, ...)).
            return ForbiddenCheckSchema(rule_violations=list(self._forbidden_by_text.get(prompt, [])))
        if role is Role.CRITIC:
            # Стъпка [7], async — виж докстринга на runner._start_async_final_check.
            return self._critic_response
        raise AssertionError(f"Неочаквана роля {role}")


def _write_briefing(post_dir):
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "briefing.md").write_text("Утре имаме среща в 10 часа с 5 партньора.", encoding="utf-8")


def test_v5_chain_produces_a_proposal_from_the_pairwise_winner(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    config = make_config(pipeline_chain="v5", writer_candidates=2)
    client = FakeV5Client(
        writer=["Кандидат едно.", "Кандидат два, по-конкретен."], judge_winners=["B"], config=config
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status in ("ready", "ready_with_notes")
    assert result.merged_from == ["post_2.md"]
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Кандидат два, по-конкретен."
    assert (post_dir / "facts_pool.json").exists()
    assert (post_dir / "facts_check.json").exists()
    assert (post_dir / "judgement.json").exists()
    assert result.meta_path.exists()

    time.sleep(0.3)  # изчаква фоновата [7] проверка, за да не тече неконтролируемо след теста


def test_v5_chain_flags_unverified_claims_from_the_chosen_candidate(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    config = make_config(pipeline_chain="v5", writer_candidates=2)
    client = FakeV5Client(
        writer=["Кратко.", "Лекторът е водещ експерт от Software University."],
        judge_winners=["B"],
        config=config,
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.status == "ready_with_notes"
    assert any("Software" in note or "University" in note for note in result.open_notes)

    time.sleep(0.3)


def test_v5_chain_flags_gdpr_mention_in_the_chosen_candidate(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    knowledge = make_brand_knowledge(members=[{"name": "Zariba EOOD", "mention_allowed": False}])
    config = make_config(pipeline_chain="v5", writer_candidates=2)
    client = FakeV5Client(
        writer=["Кратко без спорни имена.", "Благодарим на Zariba EOOD за домакинството."],
        judge_winners=["B"],
        config=config,
    )

    result = run_pipeline(post_dir, client, knowledge=knowledge)

    assert result.status == "ready_with_notes"
    assert any("GDPR" in note for note in result.open_notes)

    time.sleep(0.3)


def test_v5_chain_async_final_check_appends_to_meta_json_without_blocking(tmp_path):
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    config = make_config(pipeline_chain="v5", writer_candidates=2)
    client = FakeV5Client(
        writer=["Кандидат едно.", "Кандидат два."],
        judge_winners=["B"],
        config=config,
        critic_response=CritiqueSchema(verdict=Verdict.NEEDS_REVISION, tone_issues=["звучи административно"]),
    )

    started = time.monotonic()
    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())
    elapsed = time.monotonic() - started

    # Резултатът се връща веднага — стъпка [7] не е синхронна тук (виж план 15, Част 4).
    assert elapsed < 2.0
    assert "звучи административно" not in result.open_notes  # още не е стигнала до тук

    import json

    for _ in range(50):
        meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
        if any("звучи административно" in note for note in meta.get("open_notes", [])):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("Асинхронната [7] проверка не обнови meta.json навреме.")

    assert meta["status"] == "ready_with_notes"


def test_v5_chain_excludes_a_forbidden_candidate_before_judge_selects(tmp_path):
    """
    Затвореният pre-selection пропуск: кандидат с rules/forbidden.md нарушение (тук —
    връщано от лекия check_forbidden_rules, не от кода) НЕ влиза в сравнението на Съдията —
    единственият допустим кандидат печели автоматично, judge_winners остава празна опашка.
    """
    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    config = make_config(pipeline_chain="v5", writer_candidates=2)
    forbidden_text = "Нашето събитие е много по-добро от конференцията на конкурента Х."
    client = FakeV5Client(
        writer=[forbidden_text, "Чист текст без спорни сравнения."],
        judge_winners=[],  # не бива да се вика — само 1 допустим кандидат след изключването
        config=config,
        forbidden_by_text={forbidden_text: [RuleViolation(rule="forbidden", detail="критика на конкурент")]},
    )

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.merged_from == ["post_2.md"]
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Чист текст без спорни сравнения."

    time.sleep(0.3)


def test_default_chain_is_still_v3_when_config_omits_pipeline_chain(tmp_path):
    """Регресионна ограда: без PIPELINE_CHAIN в .env, run_pipeline трябва да остане на v3."""
    from tests.test_runner_verdicts import CLEAN_CRITIQUE, make_client

    post_dir = tmp_path / "post"
    _write_briefing(post_dir)
    client = make_client(writer=["Пост."], critic=[CLEAN_CRITIQUE, CLEAN_CRITIQUE], consolidator=["Пост."])

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge())

    assert result.merged_from == ["post_i.md"]  # v3-специфично име на файл, не post_N.md
