"""
Тестове за поправките от research-plan-36 (пълна оценка: скорост, адекватност, интуитивност).
Всеки тест назовава точката от плана, която пази — А (без риск), Б (скорост),
В (интуитивност), Г (адекватност). Без мрежа: фалшиви клиенти, както в останалите тестове.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

import src.llm.client as client_module
from src.brand.format_rules import FormatRules, align_with_examples
from src.errors import AuthenticationError, CancelledByUserError
from src.humanize import humanize_note, humanize_notes, status_label
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import (
    CritiqueSchema,
    MediaSelectionSchema,
    PlanSchema,
    Verdict,
)
from src.pipeline import critic
from src.pipeline.consolidator import PipelineResult
from src.pipeline.preview import render_preview
from src.pipeline.runner import _fact_loss_note, run_pipeline, run_revision
from src.quality import bench
from src.quality.metrics import edit_ratio
from src.repl import Ink, Repl, looks_like_revision, unsupported_file_paths
from tests.conftest import content_response, make_brand_knowledge, make_config

PLAN = PlanSchema(
    post_type="събитие", format="само текст", tone="топло", needs_photos=False, max_photo_count=0, is_sensitive=False
)
CLEAN = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")


class SequencedClient:
    """Предварително зададени отговори по роля; гърми при неочаквано извикване."""

    def __init__(self, responses: dict[Role, list[Any]]) -> None:
        self._queues = {role: list(values) for role, values in responses.items()}
        self.prompts: dict[Role, list[str]] = {}
        self.call_log: list[Any] = []

    def complete(self, role, prompt, *, system=None, images=None, schema=None):
        queue = self._queues.get(role)
        if not queue:
            raise AssertionError(f"неочаквано извикване на {role}")
        self.prompts.setdefault(role, []).append(prompt)
        return queue.pop(0)


def _brief(post_dir: Path, text: str = "Проведохме среща с 15 фирми на 20 август 2026 г.") -> None:
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "briefing.md").write_text(text, encoding="utf-8")


# ============================================================================ В2 — човешки бележки


def test_member_without_consent_from_brief_becomes_a_plain_reminder():
    note = "[GDPR] Организацията 'Зариба' е от брифа и остава в текста — проверете ръчно, че имат съгласие"
    text = humanize_note(note)
    assert "„Зариба“" in text
    assert "members.yaml" not in text and "mention_allowed" not in text


def test_unverified_claim_and_forbidden_rule_are_actionable():
    assert humanize_note('Недоказано твърдение: "40 членове" (няма в брифа)') == (
        "Проверете този факт — не е от брифа: „40 членове“"
    )
    forbidden = humanize_note("[rules/forbidden.md] Критика на конкурент.")
    assert "rules/forbidden.md" not in forbidden
    assert "правилата на клъстера" in forbidden


def test_internal_notes_without_action_are_hidden_and_duplicates_removed():
    notes = [
        "Консолидаторът върна post_ii.md дословно — синтез между двата поста не се е случил.",
        "ЧУВСТВИТЕЛНА ТЕМА — Планерът прецени...",
        "ЧУВСТВИТЕЛНА ТЕМА — Планерът прецени...",
    ]
    assert humanize_notes(notes) == [
        "Чувствителна тема — прочетете поста внимателно и го дайте за одобрение преди публикуване."
    ]



def test_old_minor_photo_warning_is_hidden():
    """Засичането на непълнолетни е махнато (2026-10-15) — старите предупреждения в meta.json не се показват."""
    old = ("Избрана снимка е маркирана от Media Picker като вероятно съдържаща непълнолетни лица — "
           "прегледайте ръчно преди публикуване.")
    assert humanize_note(old) is None
    assert humanize_notes([old]) == []

def test_unknown_note_passes_through_with_jargon_softened():
    assert humanize_note("Бележка за facts.yaml и Media Picker") == "Бележка за данните за клъстера и изборът на снимки"


def test_status_labels_are_bulgarian():
    assert status_label("ready") == "Готов за публикуване"
    assert "бележките" in status_label("ready_with_notes")


# ============================================================================ В3 / А9 — вход


@pytest.mark.parametrize(
    "line, expected",
    [
        ("по-кратко", True),
        ("Добави благодарност на ВСУ", True),
        ("махни втория абзац", True),
        ("Добавихме нов член в клъстера", False),  # минало време — истински бриф
        ("Проведохме среща с 15 фирми", False),
        ("добави " + "дума " * 40, False),  # дълъг текст е по-скоро нов бриф
    ],
)
def test_revision_detection_only_matches_short_imperatives(line, expected):
    assert looks_like_revision(line) is expected


def test_unsupported_file_is_recognised_instead_of_becoming_a_brief(tmp_path):
    heic = tmp_path / "IMG_0042.HEIC"
    heic.write_bytes(b"x")
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"x")

    assert unsupported_file_paths(f'"{heic}"') == [heic]
    assert unsupported_file_paths(f'"{jpg}"') is None  # поддържана снимка — друг път
    assert unsupported_file_paths("Проведохме среща") is None


# ============================================================================ Г1 — правила от реалните постове


def test_rules_are_widened_to_fit_every_verbatim_post():
    rules = FormatRules(allowed_emoji=frozenset({"🙏"}), emoji_min=3, emoji_max=6, words_min=130, words_max=400)
    examples = ["едно " * 103, "🙏 🤝 🔹 🔹 🔹 🔹 📖 " + "дума " * 110]

    aligned = align_with_examples(rules, examples)

    assert {"🙏", "🤝", "🔹", "📖"} <= aligned.allowed_emoji
    assert (aligned.emoji_min, aligned.emoji_max) == (0, 7)
    assert aligned.words_min == 100  # 103 → закръглено надолу до десетица
    assert aligned.words_max == 400  # по-широкото правило остава


def test_rules_are_unchanged_without_examples():
    rules = FormatRules(emoji_min=3, emoji_max=6, words_min=130)
    assert align_with_examples(rules, []) is rules


# ============================================================================ Г2 — име от брифа остава


def _critique_for(draft: str, brief: str) -> CritiqueSchema:
    critique = CritiqueSchema(verdict=Verdict.OK, facts=[], tone_issues=[], rule_violations=[], summary="")
    members = [{"name": "Зариба", "mention_allowed": None}]
    critic.enforce_code_level_rules(critique, draft, brief, members, ["ИКТ Клъстер Варна"])
    return critique


def test_member_named_in_the_brief_is_not_sent_to_post_ii_for_removal():
    critique = _critique_for("Благодарим на Зариба за домакинството.", "Благодарим на Зариба за домакинството.")

    assert any(v.from_brief for v in critique.rule_violations)
    assert not any("Зариба" in finding for finding in critic.collect_findings(critique))
    assert any("Зариба" in note for note in critic.review_notes(critique))  # човекът пак вижда бележка


def test_member_added_by_the_model_is_still_removed():
    critique = _critique_for("Благодарим на Зариба за домакинството.", "Благодарим на домакините.")

    assert not any(v.from_brief for v in critique.rule_violations)
    assert any("махни" in finding and "Зариба" in finding for finding in critic.collect_findings(critique))


def test_latin_org_from_the_brief_is_kept_but_reported():
    brief = "Благодарим на TechVenture GmbH за подкрепата."
    critique = _critique_for(brief, brief)
    org = [v for v in critique.rule_violations if "TechVenture" in v.detail]
    assert org and org[0].from_brief
    assert not any("TechVenture" in finding for finding in critic.collect_findings(critique))


# ============================================================================ Г3 — загуба на факти


def test_fact_loss_note_fires_only_on_a_real_loss():
    rich = "На 20 август 2026 г. 15 фирми, ВСУ, ТУ Варна и Зариба се събраха в Варна. 76 участници, 27 отбора."
    poor = "Проведохме среща с партньори."
    assert _fact_loss_note(rich, poor) is not None
    assert _fact_loss_note(rich, rich) is None
    assert _fact_loss_note("Среща с ВСУ.", "Среща.") is None  # малка разлика не е загуба


# ============================================================================ А5 / А8 / Б3 / Б4 — клиент


def test_rejected_api_key_stops_immediately_without_retries_or_fallbacks(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["model"])
        response = requests.Response()
        response.status_code = 401
        raise requests.exceptions.HTTPError("401 Unauthorized", response=response)

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(), sleep_fn=lambda s: None)

    with pytest.raises(AuthenticationError) as exc_info:
        client.complete(Role.WRITER, "промпт")

    assert len(calls) == 1
    assert "/ключ" in exc_info.value.human_message


def test_optional_speedups_are_off_by_default_and_scoped_when_on(monkeypatch):
    bodies = []

    def fake_post(url, headers, json, timeout, **kwargs):  # stream=True при гарантиран JSON (план 38)
        bodies.append(json)
        return content_response('{"post_type": "x", "format": "само текст", "tone": "t", "needs_photos": false, '
                                '"max_photo_count": 0, "is_sensitive": false}')

    monkeypatch.setattr(client_module.requests, "post", fake_post)

    LLMClient(make_config()).complete(Role.PLANNER, "бриф", schema=PlanSchema)
    assert "chat_template_kwargs" not in bodies[-1] and "response_format" not in bodies[-1]

    tuned = LLMClient(make_config(no_thinking_roles=frozenset({"planner"}), structured_output="json_schema"))
    tuned.complete(Role.PLANNER, "бриф", schema=PlanSchema)
    assert bodies[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[-1]["response_format"]["json_schema"]["name"] == "PlanSchema"

    tuned.complete(Role.WRITER, "текст")  # друга роля, без схема — нищо от двете
    assert "chat_template_kwargs" not in bodies[-1] and "response_format" not in bodies[-1]


def test_network_attempts_are_logged_separately(monkeypatch):
    attempts = {"n": 0}

    def fake_post(url, headers, json, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.Timeout("бавно")
        return content_response("готово")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(), sleep_fn=lambda s: None)
    client.complete(Role.WRITER, "промпт")

    assert client.call_log[0].to_dict()["network_attempts"] == 2


# ============================================================================ Б1 / А7 / А3 / А8 — конвейер


def test_first_draft_is_shown_before_the_critic_runs(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir)
    order: list[str] = []

    class Client(SequencedClient):
        def complete(self, role, prompt, **kwargs):
            order.append(role.value)
            return super().complete(role, prompt, **kwargs)

    client = Client(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова I."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN, CLEAN],
            Role.CONSOLIDATOR: ["Предложение."],
        }
    )

    def on_draft(text: str) -> None:
        order.append(f"draft:{text}")

    run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), on_draft=on_draft)

    assert "draft:Чернова I." in order
    assert order.index("draft:Чернова I.") < order.index("critic")


def test_cancel_stops_at_the_next_step_boundary(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir)
    client = SequencedClient({Role.PLANNER: [PLAN]})  # всяко следващо извикване би гръмнало

    with pytest.raises(CancelledByUserError):
        run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), should_cancel=lambda: True)


def test_pipeline_records_its_duration_in_meta(tmp_path):
    post_dir = tmp_path / "post"
    _brief(post_dir)
    client = SequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN, CLEAN],
            Role.CONSOLIDATOR: ["Предложение."],
        }
    )
    result = run_pipeline(post_dir, client, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=5)

    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert isinstance(meta["pipeline_seconds"], float)


def test_final_check_after_a_rerun_does_not_reuse_the_old_critique(tmp_path):
    """А3: преди стъпка [7] намираше critique_final.md от предишното пускане и връщаше
    неговите бележки за текст, който вече не съществува."""
    post_dir = tmp_path / "post"
    _brief(post_dir)
    stale = CritiqueSchema(verdict=Verdict.NEEDS_REVISION, facts=[], tone_issues=["СТАРА бележка"], rule_violations=[])
    (post_dir / "critique_final.md").write_text(critic._render_critique_markdown(stale), encoding="utf-8")
    client = SequencedClient(
        {
            Role.PLANNER: [PLAN],
            Role.WRITER: ["Чернова."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CRITIC: [CLEAN, CLEAN],
            Role.CONSOLIDATOR: ["Предложение."],
        }
    )
    run_pipeline(post_dir, client, force=True, knowledge=make_brand_knowledge(), wait_for_final_check_seconds=5)

    meta = json.loads((post_dir / "output" / "meta.json").read_text(encoding="utf-8"))
    assert "СТАРА бележка" not in meta["open_notes"]
    assert len(client.prompts[Role.CRITIC]) == 2  # финалната проверка наистина е извикала модела


# ============================================================================ Б2 — лека ревизия


def _finished_post(tmp_path: Path) -> Path:
    post_dir = tmp_path / "post"
    _brief(post_dir)
    (post_dir / "plan.json").write_text(PLAN.model_dump_json(), encoding="utf-8")
    (post_dir / "media.json").write_text(MediaSelectionSchema(selected=[]).model_dump_json(), encoding="utf-8")
    (post_dir / "output").mkdir()
    (post_dir / "output" / "post.md").write_text("Стар текст за срещата.\n", encoding="utf-8")
    return post_dir


def test_light_revision_edits_the_existing_post_with_one_writer_call(tmp_path):
    post_dir = _finished_post(tmp_path)
    client = SequencedClient({Role.WRITER: ["Нов, по-кратък текст."], Role.CRITIC: [CLEAN]})

    result = run_revision(
        post_dir, client, "по-кратко", knowledge=make_brand_knowledge(), wait_for_final_check_seconds=5
    )

    assert (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip() == "Нов, по-кратък текст."
    assert (post_dir / "output" / "post.v1.md").read_text(encoding="utf-8").strip() == "Стар текст за срещата."
    prompt = client.prompts[Role.WRITER][0]
    assert "Стар текст за срещата." in prompt and "по-кратко" in prompt  # Писателят вижда готовия пост
    assert Role.PLANNER not in client.prompts and Role.CONSOLIDATOR not in client.prompts
    assert result.merged_from == ["ревизия"]


def test_light_revision_falls_back_to_a_full_run_for_an_insufficient_brief(tmp_path):
    post_dir = _finished_post(tmp_path)
    insufficient = PLAN.model_copy(update={"insufficient_brief": True})
    (post_dir / "plan.json").write_text(insufficient.model_dump_json(), encoding="utf-8")
    client = SequencedClient(
        {
            Role.PLANNER: [insufficient],
            Role.WRITER: ["Честен отказ."],
            Role.MEDIA: [MediaSelectionSchema(selected=[])],
            Role.CONSOLIDATOR: ["Честен отказ."],
        }
    )

    result = run_revision(post_dir, client, "добави дата", knowledge=make_brand_knowledge())

    assert result.insufficient_brief is True  # мина през пълния конвейер (force=True)


# ============================================================================ REPL — В1/В3/В4/А6/А7/Г6


class RecordingPipeline:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _result(self, post_dir: Path) -> PipelineResult:
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост за срещата.\n", encoding="utf-8")
        return PipelineResult(
            status="ready", output_dir=output_dir, post_path=output_dir / "post.md", meta_path=output_dir / "meta.json"
        )

    def __call__(self, post_dir, client, *, force=False, **kwargs):
        self.calls.append({"kind": "full", "post_dir": post_dir, "brief": (post_dir / "briefing.md").read_text("utf-8")})
        if kwargs.get("on_draft"):
            kwargs["on_draft"]("Първа чернова.")
        return self._result(post_dir)

    def revise(self, post_dir, client, instruction, **kwargs):
        self.calls.append({"kind": "revision", "post_dir": post_dir, "instruction": instruction})
        return self._result(post_dir)

    def refresh(self, post_dir, client, **kwargs):
        self.calls.append({"kind": "photos", "post_dir": post_dir})
        return self._result(post_dir)


class FakePlannerClient:
    def __init__(self, config) -> None:
        self.config = config
        self.call_log: list = []

    def complete(self, role, prompt, *, system=None, schema=None, **kwargs):
        return PLAN


def _repl(tmp_path, lines, *, pending=None, pipeline=None):
    output: list[str] = []
    inputs = iter(lines)
    pending_flags = iter(pending or [])

    def input_fn(_prompt):
        try:
            value = next(inputs)
        except StopIteration:
            raise EOFError
        if isinstance(value, BaseException):
            raise value
        return value

    pipeline = pipeline or RecordingPipeline()
    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakePlannerClient,
        pipeline_fn=pipeline,
        revision_fn=pipeline.revise,
        photo_refresh_fn=pipeline.refresh,
        open_preview_fn=lambda path: None,
        pending_input_fn=lambda: next(pending_flags, False),
        today=date(2026, 10, 9),
    )
    return repl, output, pipeline


def test_pasted_multiline_text_becomes_one_brief(tmp_path):
    lines = ["Здравейте,", "вчера проведохме среща с 15 фирми.", "Поздрави"]
    repl, _, pipeline = _repl(tmp_path, lines, pending=[True, True, False])

    repl.run()

    assert len(pipeline.calls) == 1
    assert "15 фирми" in pipeline.calls[0]["brief"] and "Поздрави" in pipeline.calls[0]["brief"]


def test_ctrl_c_at_the_prompt_does_not_end_the_session(tmp_path):
    repl, output, pipeline = _repl(tmp_path, [KeyboardInterrupt(), "Среща с фирми"])

    repl.run()

    assert len(pipeline.calls) == 1
    assert any("/exit" in line for line in output)


def test_plain_revision_text_after_a_post_edits_it(tmp_path):
    repl, output, pipeline = _repl(tmp_path, ["Среща с 15 фирми", "по-кратко"])

    repl.run()

    assert [call["kind"] for call in pipeline.calls] == ["full", "revision"]
    assert pipeline.calls[1]["instruction"] == "по-кратко"


def test_new_keyword_with_colon_forces_a_new_post(tmp_path):
    repl, _, pipeline = _repl(tmp_path, ["Среща с 15 фирми", "нов: Добави ВСУ като партньор"])

    repl.run()

    assert [call["kind"] for call in pipeline.calls] == ["full", "full"]
    assert pipeline.calls[1]["brief"].strip() == "Добави ВСУ като партньор"


def test_draft_is_printed_and_progress_hides_internals_by_default(tmp_path):
    repl, output, _ = _repl(tmp_path, ["Среща с 15 фирми"])

    repl.run()

    text = "\n".join(output)
    assert "Чернова — още я проверявам" in text
    assert "Първа чернова." in text
    assert "писател:" not in text  # банерът е без ID на модела (В5)


def test_photos_added_after_a_post_can_go_to_that_post(tmp_path):
    photo = tmp_path / "снимка.jpg"
    photo.write_bytes(b"x")
    repl, _, pipeline = _repl(tmp_path, ["Среща с 15 фирми", f'"{photo}"', ""])

    repl.run()

    assert [call["kind"] for call in pipeline.calls] == ["full", "photos"]
    assert (pipeline.calls[0]["post_dir"] / "photos" / "снимка.jpg").exists()


def test_published_text_is_saved_and_edit_ratio_reported(tmp_path):
    repl, output, pipeline = _repl(tmp_path, ["Среща с 15 фирми", "/publikuvano Готов пост за срещата с ВСУ."])

    repl.run()

    published = pipeline.calls[0]["post_dir"] / "output" / "published.md"
    assert published.read_text(encoding="utf-8").strip() == "Готов пост за срещата с ВСУ."
    assert any("% от текста е променен" in line for line in output)


def test_empty_api_key_is_not_written(tmp_path):
    env_path = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("OPENWEBUI_API_KEY=\n", encoding="utf-8")
    inputs = iter(["", ""])  # „да" на въпроса, празен ключ

    def input_fn(_prompt):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakePlannerClient,
        pipeline_fn=RecordingPipeline(),
        env_path=env_path,
        env_example_path=example,
    )
    repl.run()

    assert not env_path.exists()  # следващото стартиране ще попита пак


# ============================================================================ В6 / Г6 / Г7


def test_preview_is_in_bulgarian_without_sponsored_label(tmp_path):
    post_dir = tmp_path / "post"
    (post_dir / "output").mkdir(parents=True)
    (post_dir / "output" / "post.md").write_text("Текст.\n", encoding="utf-8")
    meta = {"status": "ready_with_notes", "open_notes": ['Недоказано твърдение: "40 членове" (няма)'], "warnings": []}
    (post_dir / "output" / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    html_text = render_preview(post_dir).read_text(encoding="utf-8")

    body = html_text.split("<body>", 1)[1]
    assert "Спонсорирано" not in body
    assert "<b>Готов — прегледайте бележките</b>" in body
    assert "<b>ready_with_notes</b>" not in body
    assert "Проверете този факт" in body
    assert "Недоказано твърдение" not in body  # суровият текст остава само в meta.json


def test_desktop_guard_blocks_clipboard_browser_and_file_manager():
    """
    Изрично искане на потребителя (2026-10-12): тестовете никога не пипат браузъра или
    клипборда му. Пази самата защита в tests/conftest.py — истинска команда за клипборд и
    отваряне на браузър/файл НЕ се изпълняват, а се записват.
    """
    import os
    import subprocess
    import webbrowser

    violations = subprocess.Popen.__init__.__globals__["_desktop_violations"]
    before = len(violations)

    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value 'ТЕСТ'"], check=True)
    webbrowser.open("https://example.test")
    if hasattr(os, "startfile"):
        os.startfile("C:\\")

    recorded = violations[before:]
    del violations[before:]  # очаквани в този тест — не бива да провалят цялата сесия
    assert any("powershell" in item for item in recorded)  # командата за клипборд не е изпълнена
    # Браузърът и файловият мениджър се спират още от подмяната във всеки тест; и двата слоя
    # са в conftest — истинската функция от стандартната библиотека не е достъпна.
    assert webbrowser.open.__module__ != "webbrowser"
    if hasattr(os, "startfile"):
        assert getattr(os.startfile, "__module__", "nt") != "nt"


def test_edit_ratio():
    assert edit_ratio("едно две три четири", "едно две три четири") == 0.0
    assert 0 < edit_ratio("едно две три четири", "едно две пет четири") < 1


def test_bench_reads_published_text_and_v5_runs(tmp_path, monkeypatch):
    posts = tmp_path / "posts"
    v3 = posts / "a-v3"
    (v3 / "output").mkdir(parents=True)
    (v3 / "post_i.md").write_text("Чернова на 20 август.", encoding="utf-8")
    (v3 / "output" / "post.md").write_text("Среща на 20 август с 15 фирми.", encoding="utf-8")
    (v3 / "output" / "published.md").write_text("Среща на 20 август с 15 фирми.", encoding="utf-8")
    (v3 / "output" / "meta.json").write_text('{"status": "ready", "pipeline_seconds": 120.5}', encoding="utf-8")
    v5 = posts / "b-v5"
    (v5 / "output").mkdir(parents=True)
    (v5 / "output" / "post.md").write_text("Избран кандидат за срещата.", encoding="utf-8")
    monkeypatch.setattr(bench, "load_brand_knowledge", make_brand_knowledge)

    report = bench.collect(posts)
    agg = report.aggregate()

    assert len(report.posts) == 2  # v5 вече не се пропуска
    assert agg["published_count"] == 1 and agg["published_unchanged"] == 1
    assert agg["pipeline_seconds_median"] == 120.5
