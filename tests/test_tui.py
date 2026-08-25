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
from src.tui import AgentTUI
from tests.conftest import make_brand_knowledge, make_config


class FakePipeline:
    """Записва извикванията и пише output/post.md — огледално на test_repl.py::FakePipeline."""

    def __init__(self, *, status: str = "ready", raises: Exception | None = None):
        self.calls: list[dict] = []
        self.status = status
        self.raises = raises

    def __call__(self, post_dir: Path, client, *, force: bool = False, knowledge=None, **kwargs) -> PipelineResult:
        self.calls.append({"post_dir": post_dir, "force": force, "knowledge": knowledge, "mode": kwargs.get("mode")})
        if self.raises is not None:
            raise self.raises
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


def make_app(tmp_path: Path, pipeline: FakePipeline) -> AgentTUI:
    return AgentTUI(
        posts_dir=tmp_path / "posts",
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=pipeline,
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
