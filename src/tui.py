"""
Пълен full-screen TUI, върху Textual — по потребителска заявка за "премиум" изглед
(2026-08-17). Съществува ЗАЕДНО с src/repl.py (readline-стил, ред-по-ред от 2026-08-11),
не го заменя — активира се изрично с `python -m src.cli tui`. repl.py остава подразбиращият
се начин на работа (`python -m src.cli` без аргумент); нищо в него не е пипнато.

Защо отделен модул, не преправяне на repl.py: Textual е различен модел на изпълнение
(event loop, widgets, async workers), не читаема добавка върху readline цикъла. Двата пътя
споделят логиката на конвейера (src/pipeline/runner.py) и малките помощници на repl.py
(slugify, unique_post_dir, extract_inline_attachments, copy_attachments) — не я дублират.

Единствената нова зависимост е `textual` (виж pyproject.toml); rich вече беше налична.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, ProgressBar, RichLog, Static

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.config import Config, ConfigError, load_config
from src.errors import AgentError
from src.llm.client import LLMClient
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline.runner import run_pipeline
from src.pipeline.util import read_text_or_none
from src.repl import (
    _STEP_ORDER,
    _STEP_TAG_RE,
    copy_attachments,
    extract_inline_attachments,
    slugify,
    unique_post_dir,
)

POSTS_DIR = Path("posts")


class _TuiLogHandler(logging.Handler):
    """
    Препраща run_pipeline() логовете (INFO+) към TUI-то от фоновата нишка на worker-а.
    `call_from_thread` е задължителен тук — Textual widget-ите не са thread-safe за пряка
    промяна извън главния event loop (виж докстринга на App.call_from_thread).
    """

    def __init__(self, app: "AgentTUI") -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        match = _STEP_TAG_RE.match(message)
        tag = match.group(1) if match else None
        is_warning = record.levelno >= logging.WARNING
        self._app.call_from_thread(self._app.handle_log_line, tag, message, is_warning)


class AgentTUI(App):
    """Едноекранно приложение: лог отгоре, готов текст под него, прогрес-бар, вход отдолу."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #log {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    #output {
        height: auto;
        max-height: 45%;
        border: round $success;
        padding: 1 2;
        display: none;
    }
    #output.visible {
        display: block;
    }
    #progress {
        margin: 0 1;
    }
    #brief {
        margin: 0 1 1 1;
    }
    """

    BINDINGS = [("ctrl+q", "quit", "Изход")]
    TITLE = "ИКТ Клъстер – Варна · LinkedIn агент"

    def __init__(
        self,
        *,
        posts_dir: Path = POSTS_DIR,
        config_loader=load_config,
        knowledge_loader=load_brand_knowledge,
        client_factory=LLMClient,
        pipeline_fn=run_pipeline,
    ) -> None:
        # Инжектирани зависимости, огледално на src/repl.py::Repl.__init__ — за да могат
        # тестовете (tests/test_tui.py) да подменят модел/конфигурация/конвейер без
        # реална мрежа, .env или диск извън tmp_path.
        super().__init__()
        self.posts_dir = posts_dir
        self.config_loader = config_loader
        self.knowledge_loader = knowledge_loader
        self.client_factory = client_factory
        self.pipeline_fn = pipeline_fn
        self.config: Config | None = None
        self.knowledge: BrandKnowledge | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
            yield Static("", id="output")
            yield ProgressBar(id="progress", total=len(_STEP_ORDER), show_eta=False)
            yield Input(placeholder="Опиши повода за поста (@път за снимка) и Enter…", id="brief")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        try:
            self.config = self.config_loader()
        except ConfigError as exc:
            log.write(f"[bold red]Грешка в конфигурацията: {exc}[/]")
            return
        self.knowledge = self.knowledge_loader()
        log.write("[bold cyan]Готово. Напиши брифа долу и натисни Enter.[/]")
        self.query_one("#brief", Input).focus()

    # ------------------------------------------------------------------ прогрес от лога

    def handle_log_line(self, tag: str | None, message: str, is_warning: bool) -> None:
        """Вика се от _TuiLogHandler през call_from_thread — вече на главната нишка."""
        log = self.query_one("#log", RichLog)
        bar = self.query_one("#progress", ProgressBar)
        if tag and tag in _STEP_ORDER:
            bar.update(progress=_STEP_ORDER.index(tag) + 1)
            log.write(f"[bold]▸ {message}[/]")
            return
        style = "bold yellow" if is_warning else "dim"
        bullet = "⚠" if is_warning else "·"
        log.write(f"[{style}]    {bullet} {message}[/]")

    # ------------------------------------------------------------------ генериране

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw or self.config is None:
            return

        brief, photos, unresolved = extract_inline_attachments(raw)
        if not brief:
            self.query_one("#log", RichLog).write("[bold red]Брифът е празен.[/]")
            return

        input_widget = self.query_one("#brief", Input)
        input_widget.disabled = True
        input_widget.value = ""

        post_dir = unique_post_dir(self.posts_dir, slugify(brief), today=date.today())
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")
        copied = copy_attachments(photos, post_dir / "photos")

        log = self.query_one("#log", RichLog)
        output = self.query_one("#output", Static)
        output.remove_class("visible")
        bar = self.query_one("#progress", ProgressBar)
        bar.update(progress=0)
        log.write(f"[bold]▸ Нов пост: {post_dir}[/]" + (f" · {len(copied)} снимка(и)" if copied else ""))
        for item in unresolved:
            log.write(f"[dim](@{item} не е съществуваща снимка — остава като текст в брифа)[/]")

        self.run_worker(lambda: self._generate(post_dir), thread=True, exclusive=True, group="generate")

    def _generate(self, post_dir: Path) -> None:
        """Тече във фонова нишка (Textual worker, thread=True) — не блокира event loop-а."""
        assert self.config is not None
        client = self.client_factory(self.config)
        handler = _TuiLogHandler(self)
        register_persistent_handler(handler)
        try:
            result = self.pipeline_fn(post_dir, client, knowledge=self.knowledge)
        except AgentError as exc:
            self.call_from_thread(self._show_error, post_dir, exc)
        else:
            self.call_from_thread(self._show_result, result)
        finally:
            unregister_persistent_handler(handler)
            self.call_from_thread(self._reenable_input)

    def _show_result(self, result) -> None:
        text = (read_text_or_none(result.post_path) or "").strip()
        output = self.query_one("#output", Static)
        output.update(text)
        output.add_class("visible")

        log = self.query_one("#log", RichLog)
        status = "[bold green]✓ ready[/]" if result.status == "ready" else "[bold yellow]⚠ ready_with_notes[/]"
        log.write(f"{status} [dim]{result.post_path}[/]")
        if result.resolution_summary:
            log.write(f"[dim]{result.resolution_summary}[/]")
        for fix in result.applied_fixes:
            log.write(f"[green]✎ {fix}[/]")
        for note in result.open_notes:
            log.write(f"[yellow]⚠ {note}[/]")
        for warning in result.warnings:
            log.write(f"[yellow]⚠ {warning}[/]")

    def _show_error(self, post_dir: Path, exc: AgentError) -> None:
        log = self.query_one("#log", RichLog)
        log.write(f"[bold red]✗ Спряно: {exc.human_message}[/]")
        if exc.detail:
            log.write(f"[dim]Причина: {exc.detail}[/]")
        log.write(f"[dim]Пълен лог: {post_dir / 'run.log'}[/]")

    def _reenable_input(self) -> None:
        input_widget = self.query_one("#brief", Input)
        input_widget.disabled = False
        input_widget.focus()


def main() -> int:
    from src.terminal import force_utf8_io

    force_utf8_io()
    AgentTUI().run()
    return 0
