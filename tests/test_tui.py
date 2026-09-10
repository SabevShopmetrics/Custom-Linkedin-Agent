"""
Textual TUI (src/tui.py) — огледални тестове на test_repl.py, но през App.run_test()
вместо readline цикъл. Без pytest-asyncio: run_test() е async context manager, обвит тук
в обикновена sync тестова функция през asyncio.run().
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from src.errors import AgentError
from src.pipeline.consolidator import PipelineResult
from src.pipeline.runner import FinalCheckUpdate
from src.tui import AgentTUI
from tests.conftest import make_brand_knowledge, make_config


class FakePipeline:
    """Записва извикванията и пише output/post.md — огледално на test_repl.py::FakePipeline."""

    def __init__(self, *, status: str = "ready", raises: Exception | None = None, final_check_update=None):
        self.calls: list[dict] = []
        self.status = status
        self.raises = raises
        # Симулира стъпка [7] отговаряйки СИНХРОННО (в същия worker thread), вместо реален
        # фонов daemon thread — виж план 34, тестовете за on_final_check по-долу.
        self.final_check_update = final_check_update

    def __call__(self, post_dir: Path, client, *, force: bool = False, knowledge=None, **kwargs) -> PipelineResult:
        self.calls.append({"post_dir": post_dir, "force": force, "knowledge": knowledge, "mode": kwargs.get("mode")})
        if self.raises is not None:
            raise self.raises
        on_final_check = kwargs.get("on_final_check")
        if on_final_check is not None and self.final_check_update is not None:
            on_final_check(self.final_check_update)
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост от TUI теста.\n", encoding="utf-8")
        return PipelineResult(
            status=self.status,
            output_dir=output_dir,
            post_path=output_dir / "post.md",
            meta_path=output_dir / "meta.json",
        )


class FakeClient:
    def __init__(self, config) -> None:
        self.config = config
        self.call_log: list = []


def make_app(tmp_path: Path, pipeline: FakePipeline, *, file_picker_fn=None) -> AgentTUI:
    kwargs = {}
    if file_picker_fn is not None:
        kwargs["file_picker_fn"] = file_picker_fn
    return AgentTUI(
        posts_dir=tmp_path / "posts",
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=pipeline,
        # no-op: без него всяко успешно генериране в тестовете би отворило РЕАЛЕН браузър
        # (виж AgentTUI._show_result — автоматично отваряне на preview.html, потребителска
        # заявка 2026-09-03).
        open_preview_fn=lambda path: None,
        **kwargs,
    )


def run(coro):
    return asyncio.run(coro)


def test_app_mounts_and_shows_ready_message(tmp_path):
    async def scenario():
        app = make_app(tmp_path, FakePipeline())
        async with app.run_test() as pilot:
            await pilot.pause()
            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "Готово" in text

    run(scenario())


def test_ctrl_c_exits_the_app(tmp_path):
    """Потребителска инструкция 2026-08-17: Ctrl+C трябва да излиза, не само Ctrl+Q."""

    async def scenario():
        app = make_app(tmp_path, FakePipeline())
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.is_running
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running

    run(scenario())


def test_submitting_a_brief_runs_the_pipeline_and_shows_the_post(tmp_path):
    async def scenario():
        pipeline = FakePipeline()
        app = make_app(tmp_path, pipeline)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#brief")
            await pilot.press(*"Тестово събитие")
            await pilot.press("enter")
            # изчаква фоновия worker (thread=True) да приключи
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert len(pipeline.calls) == 1
            output = app.query_one("#output")
            assert "visible" in output.classes
            output_text = app.query_one("#output-text")
            assert "Готов пост от TUI теста." in str(output_text.content)

    run(scenario())


def test_attach_button_picks_photos_and_copies_them_into_the_post(tmp_path):
    """
    "📎 Прикачи снимки" — истинска кликаема икона с мишка (потребителска заявка,
    2026-09-02: "искам да е там като иконка и при натискане да прикачвам снимки"),
    аналог на бутона в src/repl.py, чрез инжектиран file_picker_fn (виж make_app).
    """
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    app = make_app(tmp_path, pipeline, file_picker_fn=lambda: (str(photo),))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#attach-button")
            await app.workers.wait_for_complete()
            await pilot.pause()

            status = app.query_one("#attach-status")
            assert "1 снимка" in str(status.content)

            await pilot.click("#brief")
            await pilot.press(*"Пост с прикачена снимка")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            post_dir = pipeline.calls[0]["post_dir"]
            assert (post_dir / "photos" / "a.jpg").exists()
            # изчистено след изпращане — не се пренася към следващ пост
            assert "снимка" not in str(app.query_one("#attach-status").content)

    run(scenario())


def test_attach_button_with_nothing_selected_stays_quiet(tmp_path):
    app = make_app(tmp_path, FakePipeline(), file_picker_fn=lambda: ())

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#attach-button")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert str(app.query_one("#attach-status").content) == ""
            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "не е наличен" not in text

    run(scenario())


def test_attach_button_without_available_dialog_points_to_the_issue(tmp_path):
    """Среда без графичен интерфейс (file_picker_fn връща None) — ясно съобщение в лога,
    не тихо мълчание и не срив на приложението."""
    app = make_app(tmp_path, FakePipeline(), file_picker_fn=lambda: None)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#attach-button")
            await app.workers.wait_for_complete()
            await pilot.pause()

            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "не е наличен" in text

    run(scenario())


def test_mode_command_switches_mode_and_is_passed_to_the_pipeline(tmp_path):
    async def scenario():
        pipeline = FakePipeline()
        app = make_app(tmp_path, pipeline)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#brief")
            await pilot.press(*"/mode панорама")
            await pilot.press("enter")
            await pilot.pause()

            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "Панорама" in text
            assert app.mode == "panorama"
            assert pipeline.calls == []  # /mode е команда, не бриф — не тръгва конвейер

            await pilot.press(*"Тестово събитие")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert len(pipeline.calls) == 1
            assert pipeline.calls[0]["mode"] == "panorama"

    run(scenario())


def test_agent_error_is_shown_without_crashing(tmp_path):
    async def scenario():
        pipeline = FakePipeline(raises=AgentError("тестова грешка", detail="детайл"))
        app = make_app(tmp_path, pipeline)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#brief")
            await pilot.press(*"Друго събитие")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "Спряно" in text
            assert "тестова грешка" in text
            assert "детайл" in text
            # входът трябва да се отключи пак след грешка, не да остане заключен
            assert app.query_one("#brief").disabled is False

    run(scenario())


def test_late_final_check_notes_are_shown_in_the_log(tmp_path):
    """
    План 34 — живо тестване на TUI пилота (2026-09-04) потвърди на практика известното
    ограничение (async стъпка [7] не се обновява визуално): екранът показваше чисто
    "✓ ready", докато meta.json на диска вече имаше открита фабрикувана дата. on_final_check
    затваря дупката — тук FakePipeline я симулира синхронно, вместо реален фонов thread.
    """
    async def scenario():
        update = FinalCheckUpdate(post_dir=Path("не-важно"), status="ok", notes=["клише в предложението"])
        pipeline = FakePipeline(final_check_update=update)
        app = make_app(tmp_path, pipeline)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#brief")
            await pilot.press(*"Събитие")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "async" in text
            assert "клише в предложението" in text

    run(scenario())


def test_late_final_check_failure_is_shown_not_silently_dropped(tmp_path):
    async def scenario():
        update = FinalCheckUpdate(post_dir=Path("не-важно"), status="failed", error="модел недостъпен")
        pipeline = FakePipeline(final_check_update=update)
        app = make_app(tmp_path, pipeline)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#brief")
            await pilot.press(*"Събитие")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

            log = app.query_one("#log")
            text = "\n".join(str(line) for line in log.lines)
            assert "async" in text
            assert "модел недостъпен" in text

    run(scenario())
