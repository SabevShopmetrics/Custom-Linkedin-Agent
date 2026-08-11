"""
Тестове за интерактивния режим (src/repl.py). Никакви мрежови извиквания и никакви
реални модели — конвейерът е подменен с fake, който само записва как е бил извикан.

Целта е да се провери това, което интерактивният режим добавя над `generate`:
избор на име за папката, прикачване на снимки, /revise върху същата папка с force,
и че AgentError излиза като четимо изречение, а не като traceback.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.errors import BriefingMissingError, ModelUnavailableError
from src.pipeline.consolidator import PipelineResult
from src.repl import (
    Ink,
    Repl,
    extract_inline_attachments,
    resolve_attachments,
    slugify,
    unique_post_dir,
)
from tests.conftest import make_brand_knowledge, make_config

import pytest


class FakePipeline:
    """Записва извикванията и пише output/post.md, както прави истинският Консолидатор."""

    def __init__(self, *, status: str = "ready", warnings: list[str] | None = None, raises: Exception | None = None):
        self.calls: list[dict] = []
        self.status = status
        self.warnings = warnings or []
        self.raises = raises

    def __call__(self, post_dir: Path, client, *, force: bool = False, knowledge=None, **kwargs) -> PipelineResult:
        self.calls.append({"post_dir": post_dir, "force": force, "knowledge": knowledge})
        if self.raises is not None:
            raise self.raises
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост.\n", encoding="utf-8")
        return PipelineResult(
            status=self.status,
            output_dir=output_dir,
            post_path=output_dir / "post.md",
            meta_path=output_dir / "meta.json",
            warnings=self.warnings,
        )


class FakeClient:
    def __init__(self, config) -> None:
        self.config = config
        self.call_log: list = []

    def check_connection(self) -> dict:
        return {"planner": {"model": "planner-model", "ok": True, "duration_seconds": 0.1}}


def make_repl(tmp_path: Path, pipeline: FakePipeline, lines: list[str]) -> tuple[Repl, list[str]]:
    output: list[str] = []
    inputs = iter(lines)

    def input_fn(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=pipeline,
        today=date(2026, 8, 11),
    )
    return repl, output


# --------------------------------------------------------------------------- имена


def test_slugify_transliterates_cyrillic():
    assert slugify("Стажантска програма 2026 в клъстера") == "stazhantska-programa-2026-v-klastera"


def test_slugify_falls_back_when_nothing_usable():
    assert slugify("!!! ???") == "post"


def test_unique_post_dir_suffixes_on_collision(tmp_path):
    posts = tmp_path / "posts"
    first = unique_post_dir(posts, "praktiki", today=date(2026, 8, 11))
    first.mkdir(parents=True)
    second = unique_post_dir(posts, "praktiki", today=date(2026, 8, 11))

    assert first.name == "2026-08-11-praktiki"
    assert second.name == "2026-08-11-praktiki-2"


# --------------------------------------------------------------------------- снимки


def test_resolve_attachments_expands_dir_and_skips_non_photos(tmp_path):
    photos = tmp_path / "снимки"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"x")
    (photos / "b.png").write_bytes(b"x")
    (photos / "readme.txt").write_text("не е снимка", encoding="utf-8")

    found, unresolved = resolve_attachments([str(photos), str(tmp_path / "няма.jpg")])

    assert [p.name for p in found] == ["a.jpg", "b.png"]
    assert unresolved == [str(tmp_path / "няма.jpg")]


def test_extract_inline_attachments_keeps_unresolvable_token_as_text(tmp_path):
    photo = tmp_path / "екип.jpg"
    photo.write_bytes(b"x")

    text, found, unresolved = extract_inline_attachments(f'Пост за екипа @"{photo}" с благодарност @всички')

    assert found == [photo]
    assert "@всички" in text  # @споменаване в бриф не бива да се яде
    assert str(photo) not in text
    assert unresolved == ["всички"]


# --------------------------------------------------------------------------- поток


def test_brief_creates_post_dir_and_runs_pipeline(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["Стажантска програма 2026"])

    assert repl.run() == 0

    assert len(pipeline.calls) == 1
    post_dir = pipeline.calls[0]["post_dir"]
    assert post_dir.name == "2026-08-11-stazhantska-programa-2026"
    assert (post_dir / "briefing.md").read_text(encoding="utf-8").strip() == "Стажантска програма 2026"
    assert pipeline.calls[0]["force"] is False
    assert "Готов пост." in "\n".join(output)


def test_knowledge_is_loaded_once_and_passed_to_every_run(tmp_path):
    """Ефективността на интерактивния режим е точно в това — knowledge/ не се чете наново."""
    loads = []

    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Първи пост", "Втори пост"])

    def counting_loader():
        loads.append(1)
        return make_brand_knowledge()

    repl.knowledge_loader = counting_loader
    repl.run()

    assert len(loads) == 1
    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["knowledge"] is pipeline.calls[1]["knowledge"]


def test_inline_photo_is_copied_into_post_photos(tmp_path):
    photo = tmp_path / "екип.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f'Пост за екипа @"{photo}"'])

    repl.run()

    post_dir = pipeline.calls[0]["post_dir"]
    assert (post_dir / "photos" / "екип.jpg").exists()
    assert "@" not in (post_dir / "briefing.md").read_text(encoding="utf-8")


def test_attach_then_brief_uses_attachments_and_clears_them(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f"/attach {photo}", "Първи пост", "Втори пост"])

    repl.run()

    first_dir, second_dir = pipeline.calls[0]["post_dir"], pipeline.calls[1]["post_dir"]
    assert (first_dir / "photos" / "a.jpg").exists()
    assert list((second_dir / "photos").iterdir()) == []  # прикачените не се пренасят към следващия пост


def test_revise_appends_to_same_briefing_and_forces_rerun(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Стажантска програма", "/revise по-кратко, без клишета"])

    repl.run()

    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["post_dir"] == pipeline.calls[1]["post_dir"]  # същата папка, не нов пост
    assert pipeline.calls[1]["force"] is True  # брифът се промени → планът също трябва
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert "Бележка за ревизия 1" in briefing
    assert "по-кратко, без клишета" in briefing


def test_revise_without_post_does_not_call_pipeline(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/revise нещо"])

    repl.run()

    assert pipeline.calls == []
    assert "напиши бриф" in "\n".join(output)


def test_again_reruns_same_dir_with_force(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Стажантска програма", "/again"])

    repl.run()

    assert pipeline.calls[1]["post_dir"] == pipeline.calls[0]["post_dir"]
    assert pipeline.calls[1]["force"] is True


def test_agent_error_is_shown_as_sentence_not_traceback(tmp_path):
    pipeline = FakePipeline(raises=BriefingMissingError("posts/x"))
    repl, output = make_repl(tmp_path, pipeline, ["Някакъв бриф"])

    assert repl.run() == 0  # грешката в един пост не сваля сесията

    text = "\n".join(output)
    assert "Спряно:" in text
    assert "briefing.md" in text
    assert "Traceback" not in text


def test_text_from_an_interrupted_run_is_still_shown(tmp_path):
    """
    Ако конвейерът се счупи по средата, но на диска вече има текст, той се показва —
    по-полезно е от чисто съобщение за грешка. Маркира се като непълен, за да не се
    сбърка с финалното предложение.
    """

    def pipeline_that_writes_then_breaks(post_dir: Path, client, *, force=False, knowledge=None, **kwargs):
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Недовършен текст.\n", encoding="utf-8")
        raise ModelUnavailableError("consolidator", "някакъв-модел", 3)

    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.pipeline_fn = pipeline_that_writes_then_breaks
    repl.run()

    text = "\n".join(output)
    assert "Недовършен текст." in text
    assert "прекъснато пускане" in text
    assert "Спряно:" in text


def test_unexpected_error_does_not_end_the_session(tmp_path):
    pipeline = FakePipeline(raises=OSError("диск пълен"))
    repl, output = make_repl(tmp_path, pipeline, ["Първи бриф", "Втори бриф"])

    assert repl.run() == 0

    text = "\n".join(output)
    assert "Неочаквана грешка: OSError" in text
    assert len(pipeline.calls) == 2  # сесията продължава след счупения пост


def test_warnings_are_printed_after_the_post(tmp_path):
    pipeline = FakePipeline(warnings=["Снимка с вероятно непълнолетни лица."])
    repl, output = make_repl(tmp_path, pipeline, ["Пост за лагера"])

    repl.run()

    text = "\n".join(output)
    assert "Снимка с вероятно непълнолетни лица." in text
    assert text.index("Готов пост.") < text.index("Снимка с вероятно непълнолетни")


def test_backslash_continues_the_brief_on_next_line(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Първа част \\", "втора част"])

    repl.run()

    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Първа част\nвтора част"


def test_unknown_command_does_not_become_a_brief(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/несъществуваща"])

    repl.run()

    assert pipeline.calls == []
    assert "Непозната команда" in "\n".join(output)


def test_exit_stops_the_loop_before_remaining_input(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["/exit", "Този бриф не бива да се изпълни"])

    repl.run()

    assert pipeline.calls == []


def test_config_error_exits_with_code_1(tmp_path):
    from src.config import ConfigError

    def failing_loader():
        raise ConfigError("Липсват задължителни настройки в .env: OPENWEBUI_API_KEY")

    output: list[str] = []
    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=lambda _prompt: "",
        ink=Ink(enabled=False),
        config_loader=failing_loader,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=FakePipeline(),
    )

    assert repl.run() == 1
    assert "OPENWEBUI_API_KEY" in "\n".join(output)


def test_progress_lines_reach_the_console_during_a_run(tmp_path):
    """
    Persistent handler-ът трябва да преживее setup_run_logger, който чисти handlers на
    всяко пускане — иначе прогресът се вижда само в run.log, не и в терминала.
    """
    from src.logging_utils import setup_run_logger

    def pipeline_with_logging(post_dir: Path, client, *, force=False, knowledge=None, **kwargs):
        log = setup_run_logger(post_dir)  # точно както прави истинският runner
        log.info("[1] Планер...")
        log.warning("Папката photos/ е празна — постът ще съдържа само текст.")
        log.info("=== този ред е шум и не бива да излиза ===")
        log.info("Финален статус: ready (0 предупреждения)")  # резюмето след поста го казва
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост.\n", encoding="utf-8")
        return PipelineResult(
            status="ready",
            output_dir=output_dir,
            post_path=output_dir / "post.md",
            meta_path=output_dir / "meta.json",
        )

    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.pipeline_fn = pipeline_with_logging
    repl.run()

    text = "\n".join(output)
    assert "[1] Планер..." in text
    assert "Папката photos/ е празна" in text
    assert "този ред е шум" not in text
    assert "Финален статус" not in text


def test_persistent_handler_is_removed_after_the_session(tmp_path):
    """Сесията не бива да остави handler зад себе си — иначе `generate` в същия процес
    би печатал двойно."""
    import logging

    from src.logging_utils import LOGGER_NAME

    repl, _ = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.run()

    handlers = logging.getLogger(LOGGER_NAME).handlers
    assert not any(type(h).__name__ == "ProgressHandler" for h in handlers)
