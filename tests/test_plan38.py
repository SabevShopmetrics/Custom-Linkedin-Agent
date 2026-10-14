"""research-plan-38: самопоправка [8], „Готово" = проверено, таван за роли без мислене, бележка-резюме."""

from __future__ import annotations

import json
from pathlib import Path

import requests

import src.llm.client as client_module
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import CritiqueSchema, FactCheck, FactSource, MediaSelectionSchema, RuleViolation, Verdict
from src.pipeline import autofix
from src.pipeline.runner import run_pipeline
from src.web import flow
from tests.conftest import content_response, make_brand_knowledge, make_config
from tests.test_plan36 import CLEAN, PLAN, SequencedClient, _brief

BRIEF = "Проведохме среща с 15 фирми на 20 август 2026 г. във Варна."
OLD = "Проведохме среща с 15 фирми във Варна. Срещата укрепи доверието между бизнеса и науката."
CLAIM = "Срещата укрепи доверието между бизнеса и науката"


def _critique(*facts: FactCheck, rules: list[RuleViolation] | None = None) -> CritiqueSchema:
    return CritiqueSchema(verdict=Verdict.NEEDS_REVISION, facts=list(facts), rule_violations=rules or [])


UNVERIFIED = _critique(FactCheck(claim=CLAIM, source=FactSource.UNVERIFIED, note="няма го в брифа"))


# ============================================================================ autofix — кодовата защита


def test_fixable_items_are_only_quoted_claims_never_rule_descriptions():
    """Живо 2026-10-14 (бриф 04): стилова бележка, наречена „нарушено правило", се отчиташе като „махната"."""
    critique = _critique(
        FactCheck(claim="15 фирми", source=FactSource.BRIEFING),
        FactCheck(claim=CLAIM, source=FactSource.UNVERIFIED),
        FactCheck(claim="20 гости", source=FactSource.BRIEFING, hedged=True),
        rules=[RuleViolation(rule="gdpr", detail="име"), RuleViolation(rule="forbidden", detail="Първото изречение…")],
    )
    kinds = [item.kind for item in autofix.fixable_items(critique)]
    assert kinds == ["unverified", "hedged"]


def test_fix_that_removes_the_claim_is_accepted():
    items = autofix.fixable_items(UNVERIFIED)
    outcome = autofix.apply(OLD, "Проведохме среща с 15 фирми във Варна.", BRIEF, items)
    assert outcome.applied and [i.claim for i in outcome.fixed] == [CLAIM]


def test_fix_that_adds_a_new_number_or_name_is_rejected():
    items = autofix.fixable_items(UNVERIFIED)
    with_number = autofix.apply(OLD, "Проведохме среща с 15 фирми и 40 студенти във Варна.", BRIEF, items)
    with_name = autofix.apply(OLD, "Проведохме среща с 15 фирми във Варна, водена от Петров.", BRIEF, items)
    assert not with_number.applied and "40" in with_number.reason
    assert not with_name.applied and "петров" in with_name.reason


def test_fix_that_keeps_the_claim_or_guts_the_post_is_rejected():
    items = autofix.fixable_items(UNVERIFIED)
    assert not autofix.apply(OLD, OLD, BRIEF, items).applied
    assert not autofix.apply(OLD, "Среща.", BRIEF, items).applied


def test_name_already_in_the_brief_is_not_new():
    brief = BRIEF + " Домакин беше Технически университет."
    old = "Проведохме среща с 15 фирми. Тя беше прекрасна и вдъхновяваща за всички."
    new = "Проведохме среща с 15 фирми в Технически университет."
    items = autofix.fixable_items(_critique(FactCheck(claim="прекрасна и вдъхновяваща", source=FactSource.UNVERIFIED)))
    assert autofix.apply(old, new, brief, items).applied


# ============================================================================ [8] в конвейера


def _client(final: CritiqueSchema, writer_answers: list[str]) -> SequencedClient:
    return SequencedClient({
        Role.PLANNER: [PLAN],
        Role.WRITER: ["Чернова I.", *writer_answers],
        Role.MEDIA: [MediaSelectionSchema(selected=[])],
        Role.CRITIC: [CLEAN, final],
        Role.CONSOLIDATOR: [OLD],
    })


def test_final_check_findings_are_removed_by_the_agent(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir, BRIEF)
    client = _client(UNVERIFIED, ["Проведохме среща с 15 фирми във Варна."])

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=10)

    text = (post_dir / "output" / "post.md").read_text(encoding="utf-8")
    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert "доверието" not in text
    assert (post_dir / "output" / "post.v1.md").read_text(encoding="utf-8").strip() == OLD
    assert not any("Недоказано" in n for n in meta["open_notes"])
    assert meta["final_check"]["autofix"] == {"applied": True, "fixed": 1, "left": 0}
    assert any(f.startswith("Махнато след проверката") for f in meta["applied_fixes"])
    # Молбата към Писателя назовава точно твърдението.
    assert CLAIM in client.prompts[Role.WRITER][-1]


def test_rejected_fix_leaves_the_post_and_the_note(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir, BRIEF)
    client = _client(UNVERIFIED, ["Проведохме среща с 15 фирми и 300 гости във Варна."])

    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=10)

    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == OLD
    assert any("Недоказано" in n for n in meta["open_notes"])
    assert meta["final_check"]["autofix"]["applied"] is False


def test_autofix_can_be_turned_off(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir, BRIEF)
    client = _client(UNVERIFIED, [])  # второ извикване на Писателя би гръмнало
    client.config = make_config(final_autofix=False)

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=10)

    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == OLD


# ============================================================================ таван за роли без мислене


def test_no_thinking_roles_get_a_shorter_timeout(monkeypatch):
    timeouts: list[int] = []

    def fake_post(url, headers, json, timeout):
        timeouts.append(timeout)
        return content_response("готово")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(no_thinking_roles=frozenset({"critic"}), request_timeout_seconds=300))
    client.complete(Role.CRITIC, "x")
    client.complete(Role.WRITER, "x")
    assert timeouts == [90, 300]


def test_stuck_no_thinking_call_falls_back_quickly(monkeypatch):
    models: list[str] = []
    timeouts: list[int] = []

    def fake_post(url, headers, json, timeout):
        models.append(json["model"])
        timeouts.append(timeout)
        if len(models) == 1:
            raise requests.exceptions.ReadTimeout("зацикли")
        return content_response("готово")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(
        make_config(no_thinking_roles=frozenset({"critic"}), request_timeout_seconds=300), sleep_fn=lambda s: None
    )
    assert client.complete(Role.CRITIC, "x") == "готово"
    assert len(models) == 2 and models[0] != models[1]
    # Таванът е само за основния модел — резервният получава пълното време.
    assert timeouts == [90, 300]


# ============================================================================ „Преглед" — резюме на бележките


def test_note_digest_puts_the_riskiest_note_first_and_counts_the_rest():
    meta = {
        "status": "ready_with_notes",
        "open_notes": [
            "Първият ред няма число.",
            "Недоказано твърдение: \"X\" (няма източник)",
            "[GDPR] Организация без съгласие: Фирма Y",
        ],
        "warnings": [],
        "applied_fixes": ["Махнато след проверката: Z"],
    }
    digest = flow.note_digest(meta)
    assert "Организация без съгласие: Фирма Y (+2 бележки)" in digest.split(" · ")[0]
    assert "Агентът махна сам 1 недоказано твърдение" in digest


def test_note_digest_is_empty_without_notes():
    assert flow.note_digest({"open_notes": [], "warnings": [], "applied_fixes": []}) == ""


# ============================================================================ „Готово" = проверено


def test_web_job_finishes_only_after_the_final_check(tmp_path):
    from src.web import worker

    post_dir = tmp_path / "post"
    _brief(post_dir, BRIEF)
    client = _client(UNVERIFIED, ["Проведохме среща с 15 фирми във Варна."])
    events: list[dict] = []

    import src.web.worker as worker_module

    original = worker_module.runner.run_pipeline

    def run_with_knowledge(*args, **kwargs):
        return original(*args, knowledge=make_brand_knowledge(), **kwargs)

    worker_module.runner.run_pipeline = run_with_knowledge
    try:
        code = worker.run({"kind": "generate", "post_dir": str(post_dir)}, events.append, client_factory=lambda: client)
    finally:
        worker_module.runner.run_pipeline = original

    assert code == 0
    assert events[-1] == {"state": "done"}
    assert {"stage": "Последна проверка на готовия текст…"} in events
    # В момента на „done" текстът вече е поправеният.
    meta = json.loads((post_dir / "output" / "meta.json").read_text(encoding="utf-8"))
    assert meta["final_check"]["status"] == "ok"
    assert "доверието" not in Path(post_dir / "output" / "post.md").read_text(encoding="utf-8")


# ============================================================================ зациклен празен текст (стрийминг)


class _StreamResponse:
    headers = {"Content-Type": "text/event-stream"}
    status_code = 200

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self) -> None:
        pass

    def iter_lines(self):
        for chunk in self._chunks:
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}).encode()
        yield b"data: [DONE]"

    def close(self) -> None:
        self.closed = True


PLAN_JSON = '{"post_type": "x", "format": "само текст", "tone": "t", "needs_photos": false, "max_photo_count": 0, "is_sensitive": false}'


def test_structured_answer_is_read_in_chunks(monkeypatch):
    kwargs_seen: list[dict] = []

    def fake_post(url, headers, json, timeout, **kwargs):
        kwargs_seen.append({**kwargs, "body_stream": json.get("stream")})
        return _StreamResponse([PLAN_JSON[:40], PLAN_JSON[40:]])

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    from src.llm.schema import PlanSchema

    plan = LLMClient(make_config(structured_output="json_schema")).complete(Role.PLANNER, "бриф", schema=PlanSchema)
    assert plan.post_type == "x"
    assert kwargs_seen == [{"stream": True, "body_stream": True}]


def test_runaway_whitespace_is_cut_early_and_retried(monkeypatch):
    """Живо 2026-10-14: гарантиран JSON без мислене зацикли в 7 286 интервала/табулации за 150 s."""
    responses = [
        _StreamResponse(['{"post_type": "x",'] + [" \t"] * 500),  # зациклен
        _StreamResponse([PLAN_JSON]),
    ]
    calls: list[str] = []

    def fake_post(url, headers, json, timeout, **kwargs):
        calls.append(json["model"])
        return responses.pop(0)

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    from src.llm.schema import PlanSchema

    client = LLMClient(make_config(structured_output="json_schema"), sleep_fn=lambda s: None)
    plan = client.complete(Role.PLANNER, "бриф", schema=PlanSchema)
    assert plan.post_type == "x"
    assert calls == ["planner-model", "planner-model"]  # същият модел, не резервният


def test_server_without_streaming_still_works(monkeypatch):
    def fake_post(url, headers, json, timeout, **kwargs):
        return content_response(PLAN_JSON)  # без Content-Type: text/event-stream

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    from src.llm.schema import PlanSchema

    plan = LLMClient(make_config(structured_output="json_schema")).complete(Role.PLANNER, "бриф", schema=PlanSchema)
    assert plan.post_type == "x"


def test_fix_that_drops_a_real_fact_from_the_brief_is_rejected():
    """Живо 2026-10-14 (бриф 02): махнато беше цялото изречение с истинския гост-лектор."""
    brief = "Гост-лектор беше проф. Мария Тодорова (Технически университет – Варна). Присъстваха 180 души."
    intro = "На срещата присъстваха 180 души от бизнеса, университетите и училищата в региона на града."
    old = intro + "\n\nСпециален гост беше проф. Мария Тодорова (Технически университет – Варна)."
    new = intro
    items = autofix.fixable_items(_critique(FactCheck(claim="Специален гост беше проф. Мария Тодорова",
                                                      source=FactSource.UNVERIFIED)))
    outcome = autofix.apply(old, new, brief, items)
    assert not outcome.applied and "тодорова" in outcome.reason
    # Правилната поправка — само добавката е махната — минава.
    good = intro + "\n\nГост-лектор беше проф. Мария Тодорова (Технически университет – Варна)."
    assert autofix.apply(old, good, brief, items).applied
