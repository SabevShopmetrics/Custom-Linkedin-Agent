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
import os
import time
from datetime import date
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Gradient
from textual.containers import Vertical, VerticalScroll
from textual.theme import Theme
from textual.widgets import Footer, Header, Input, ProgressBar, RichLog, Static

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.config import Config, ConfigError, load_config
from src.errors import AgentError
from src.llm.client import LLMClient
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline.preview import render_preview
from src.pipeline.runner import run_pipeline
from src.pipeline.util import file_uri, read_text_or_none
from src.repl import (
    _STEP_ORDER,
    _STEP_TAG_RE,
    copy_attachments,
    extract_inline_attachments,
    slugify,
    unique_post_dir,
)

POSTS_DIR = Path("posts")

# LinkedIn Blue — единственият брандиран акцент. Заменя предишния тъмнолилав/розов градиент
# (потребителска обратна връзка 2026-08-18: "гадния purple градиент" — минималистично, само
# с цветовете на LinkedIn, и то само като акцент, не навсякъде).
_ACCENT = "#0A66C2"

# Неутрална, минималистична палитра — LinkedIn Blue (_ACCENT) е единственият цвят с
# брандирано значение, наложен само на прогрес-бара и рамките/бутоните за акцент. Остатъкът
# е сиво/черно/бяло, без декоративен градиент.
_THEME = Theme(
    name="ikt-minimal",
    primary=_ACCENT,
    secondary="#6b7280",  # неутрално сиво, не бранд цвят
    accent=_ACCENT,
    warning="#f5a524",
    error="#f43f5e",
    success="#22c55e",
    foreground="#e6e6e6",
    background="#000000",
    surface="#141414",
    panel="#1c1c1c",
    dark=True,
)


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
        background: $background;
    }
    #banner {
        height: 3;
        content-align: center middle;
        margin: 0 1;
    }
    #log {
        height: 1fr;
        border: round $primary;
        border-title-color: $secondary;
        padding: 0 1;
        margin: 0 1;
        background: $surface;
    }
    #output {
        height: auto;
        max-height: 45%;
        border: round $success;
        border-title-color: $success;
        padding: 1 2;
        margin: 1 1 0 1;
        background: $panel;
        display: none;
        scrollbar-size-vertical: 1;
    }
    #output.visible {
        display: block;
    }
    #output-text {
        height: auto;
        width: 100%;
    }
    #progress {
        margin: 1 1 0 1;
    }
    #status {
        margin: 0 1;
        color: $secondary;
        text-style: italic;
    }
    #brief {
        margin: 1 1 1 1;
        border: round $accent;
    }
    """

    # `priority=True` на ctrl+c е задължително: Input widget-ът на textual вече бинди
    # "ctrl+c" за копиране на маркиран текст (виж widgets/_input.py) и печели над обикновен
    # App-ниво binding, докато полето за брифа е на фокус (то е през цялото време). Priority
    # binding се проверява от App надолу към фокусирания widget, не обратно, затова печели.
    #
    # ctrl+c е нарочно отделно действие от ctrl+q, огледално на src/repl.py: там Ctrl+C по
    # време на конвейер прекъсва само текущия пост (REPL-ът продължава), а Ctrl+C на празен
    # промпт спира цялата сесия. Потребителска обратна връзка 2026-08-20: първоначалният
    # опит (ctrl+c == незабавен изход винаги) не отговарял на очакването — първото Ctrl+C
    # трябва само да спре течащата генерация, второто (вече на празен вход) да излезе.
    # ctrl+q остава безусловен твърд изход, ако някой иска да прескочи меката стъпка.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Изход"),
        Binding("ctrl+c", "quit_or_interrupt", "Прекъсни/Изход", priority=True),
    ]
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

        # Жив индикатор "все още работи" (виж _tick_spinner) — потребителска обратна
        # връзка 2026-08-17: без него всяко мълчание между два лог реда (напр. бавен
        # self-hosted Планер, 15-60+ сек за едно извикване) изглежда като увиснало
        # приложение, не просто бавен модел.
        self._spinner_timer = None
        self._spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spinner_index = 0
        self._gen_started: float | None = None
        self._current_step_desc = ""

        # История на изпратените брифове за навигация със стрелка ↑/↓ в полето за вход —
        # потребителска заявка, "като на CLI" (readline history в src/repl.py). `_history_index`
        # сочи текущата позиция в `_history`; == len(_history) значи "не навигирам, показвам
        # това, което съм написал". `_history_draft` пази недовършеното изречение, докато се
        # разглежда историята, за да не се загуби при връщане надолу — същия модел като bash.
        self._history: list[str] = []
        self._history_index: int = 0
        self._history_draft: str = ""

        # Проследяване на активна генерация за меко Ctrl+C прекъсване (виж
        # action_quit_or_interrupt). `_gen_token` се увеличава при всяко ново пускане И при
        # всяко прекъсване — worker-ът във фонова нишка носи копие от токена си и го подава
        # обратно през call_from_thread; ако не съвпада с текущия при връщане, резултатът е
        # закъснял (пристигнал след прекъсване) и се игнорира тихо, вместо да презапише вече
        # освободения вход или да покаже резултат, който потребителят вече е отписал.
        self._generating = False
        self._gen_token = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield Static(Text("◆ ИКТ КЛЪСТЕР — LinkedIn АГЕНТ", style=f"bold {_ACCENT}"), id="banner")
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
            # VerticalScroll, не гол Static: готовият текст може лесно да мине 45% max-height
            # (виж CSS) при по-дълъг пост — без скрол вътрешният текст просто се отрязва по средата,
            # без индикация, че има още. Потребителска обратна връзка 2026-08-20: "при дълъг текст
            # и като е наполовина разгънат не се вижда всичко".
            with VerticalScroll(id="output"):
                yield Static("", id="output-text")
            yield ProgressBar(
                id="progress",
                total=len(_STEP_ORDER),
                show_eta=False,
                # Плътен акцентен цвят, не дъга — same stop два пъти е най-простият начин да
                # накараш Gradient да рендира един-единствен цвят с наличния API.
                gradient=Gradient((0.0, _ACCENT), (1.0, _ACCENT)),
            )
            yield Static("", id="status")
            yield Input(placeholder="Опиши повода за поста (@път за снимка) и Enter…", id="brief")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(_THEME)
        self.theme = "ikt-minimal"
        self.query_one("#log", RichLog).border_title = "Ход на конвейера"

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
            # Малки букви само за показване (потребителска обратна връзка 2026-08-18: "thinking"
            # индикаторът/стъпките да се четат с малки букви) — записът в run.log остава
            # непроменен, `message` тук е локална копия само за widget-ите.
            display = message.lower()
            bar.update(progress=_STEP_ORDER.index(tag) + 1)
            log.write(f"[bold]▸ {display}[/]")
            self._current_step_desc = display
            return
        style = "bold yellow" if is_warning else "dim"
        bullet = "⚠" if is_warning else "·"
        log.write(f"[{style}]    {bullet} {message}[/]")

    def _start_spinner(self) -> None:
        self._gen_started = time.monotonic()
        self._spinner_index = 0
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(1 / 8, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._gen_started = None
        status = self.query_one("#status", Static) if self.is_running else None
        if status is not None:
            status.update("")

    def _tick_spinner(self) -> None:
        """
        Тиктака независимо от логовете — точно затова не изглежда увиснало между два
        лог реда. `set_interval` е на главния event loop, не на worker нишката, затова
        тук е безопасно да се пипат widget-ите директно (без call_from_thread).

        `is_running` проверката пази от рядък race: приложението се затваря (Ctrl+C)
        точно докато worker-ът още тече — таймерът може да тикне веднъж СЛЕД като
        екранът вече е демонтиран, преди `_stop_spinner` да е успял да го спре.
        """
        if self._gen_started is None or not self.is_running:
            return
        frame = self._spinner_frames[self._spinner_index % len(self._spinner_frames)]
        self._spinner_index += 1
        elapsed = time.monotonic() - self._gen_started
        desc = self._current_step_desc or "стартиране…"
        try:
            self.query_one("#status", Static).update(f"{frame} {desc}  ·  {elapsed:.0f}s")
        except Exception:
            pass

    # ------------------------------------------------------------------ история (↑/↓)

    def on_key(self, event: events.Key) -> None:
        """
        ↑/↓ в полето за брифа прелистват историята — Input widget-ът на textual не бинди
        тези клавиши сам (само left/right/home/end/... виж widgets/_input.py), затова
        събитието стига необезпокоявано дотук през bubbling от фокусирания widget.
        """
        if event.key not in ("up", "down"):
            return
        input_widget = self.query_one("#brief", Input)
        if not input_widget.has_focus or input_widget.disabled or not self._history:
            return

        if event.key == "up":
            if self._history_index == 0:
                return
            if self._history_index == len(self._history):
                self._history_draft = input_widget.value
            self._history_index -= 1
        else:
            if self._history_index >= len(self._history):
                return
            self._history_index += 1

        input_widget.value = (
            self._history_draft
            if self._history_index == len(self._history)
            else self._history[self._history_index]
        )
        input_widget.cursor_position = len(input_widget.value)
        event.prevent_default()
        event.stop()

    # ------------------------------------------------------------------ генериране

    def on_input_submitted(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        if not raw or self.config is None:
            return

        # Запис в историята преди всичко останало — независимо дали пускането ще успее,
        # потребителят трябва да може да го извика обратно с ↑ (виж on_key).
        if not self._history or self._history[-1] != raw:
            self._history.append(raw)
        self._history_index = len(self._history)
        self._history_draft = ""

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
        output = self.query_one("#output", VerticalScroll)
        output.remove_class("visible")
        bar = self.query_one("#progress", ProgressBar)
        bar.update(progress=0)
        log.write(f"[bold]▸ Нов пост: {post_dir}[/]" + (f" · {len(copied)} снимка(и)" if copied else ""))
        for item in unresolved:
            log.write(f"[dim](@{item} не е съществуваща снимка — остава като текст в брифа)[/]")

        self._current_step_desc = "стартиране…"
        self._generating = True
        self._gen_token += 1
        token = self._gen_token
        self._start_spinner()
        self.run_worker(lambda: self._generate(post_dir, token), thread=True, exclusive=True, group="generate")

    def _generate(self, post_dir: Path, token: int) -> None:
        """
        Тече във фонова нишка (Textual worker, thread=True) — не блокира event loop-а.
        `token` е самоличността на ТОВА пускане (виж коментара в __init__ до `_gen_token`) —
        Python не може принудително да убие нишка, затова Ctrl+C по време на генериране не
        спира реално блокиращото LLM извикване; вместо това UI-то се освобождава веднага, а
        резултатът, който евентуално дойде по-късно от изоставената нишка, се разпознава като
        закъснял (token != self._gen_token) и се игнорира тихо в _show_result/_show_error/
        _reenable_input.
        """
        assert self.config is not None
        client = self.client_factory(self.config)
        handler = _TuiLogHandler(self)
        register_persistent_handler(handler)
        try:
            result = self.pipeline_fn(post_dir, client, knowledge=self.knowledge)
        except AgentError as exc:
            self.call_from_thread(self._show_error, post_dir, exc, token)
        else:
            self.call_from_thread(self._show_result, result, token)
        finally:
            unregister_persistent_handler(handler)
            self.call_from_thread(self._reenable_input, token)

    def _show_result(self, result, token: int) -> None:
        if token != self._gen_token:
            return  # прекъснато с Ctrl+C междувременно — резултатът дойде закъснял, игнорира се
        text = (read_text_or_none(result.post_path) or "").strip()
        output = self.query_one("#output", VerticalScroll)
        self.query_one("#output-text", Static).update(text)
        output.border_title = "Готов текст"
        output.add_class("visible")
        output.scroll_home(animate=False)  # нов резултат винаги се показва от началото

        log = self.query_one("#log", RichLog)
        status = "[bold green]✓ ready[/]" if result.status == "ready" else "[bold yellow]⚠ ready_with_notes[/]"
        log.write(f"{status} [dim]файл: {file_uri(result.post_path)}[/]")

        # Автоматичен преглед след всяко успешно пускане (потребителска заявка 2026-08-18).
        # Best-effort: развален преглед не бива да съсипва вече готовия резултат по-горе.
        try:
            preview_path = render_preview(result.output_dir.parent)
            log.write(f"[dim]преглед: {file_uri(preview_path)}[/]")
        except (FileNotFoundError, OSError) as exc:
            log.write(f"[dim]⚠ Прегледът не се генерира: {exc}[/]")

        if result.resolution_summary:
            log.write(f"[dim]{result.resolution_summary}[/]")
        for fix in result.applied_fixes:
            log.write(f"[green]✎ {fix}[/]")
        for note in result.open_notes:
            log.write(f"[yellow]⚠ {note}[/]")
        for warning in result.warnings:
            log.write(f"[yellow]⚠ {warning}[/]")

    def _show_error(self, post_dir: Path, exc: AgentError, token: int) -> None:
        if token != self._gen_token:
            return  # прекъснато с Ctrl+C междувременно — грешката е за вече изоставено пускане
        log = self.query_one("#log", RichLog)
        log.write(f"[bold red]✗ Спряно: {exc.human_message}[/]")
        if exc.detail:
            log.write(f"[dim]Причина: {exc.detail}[/]")
        log.write(f"[dim]Пълен лог: {post_dir / 'run.log'}[/]")

    def on_unmount(self) -> None:
        """Спира спинъра при затваряне (Ctrl+C/Ctrl+Q), докато worker-ът още тече."""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _reenable_input(self, token: int) -> None:
        if token != self._gen_token:
            return  # вече прекъснато от Ctrl+C — входът е освободен там, това е закъсняло echo
        self._generating = False
        self._stop_spinner()
        input_widget = self.query_one("#brief", Input)
        input_widget.disabled = False
        input_widget.focus()

    def action_quit_or_interrupt(self) -> None:
        """
        Ctrl+C — огледално на src/repl.py::run(): по време на генериране прекъсва само нея
        (агентът/TUI-то остава отворено), на празен/идле вход излиза съвсем. Второ Ctrl+C
        веднага след първото (вече на идле вход, защото генерирането е спряно) излиза.
        """
        if self._generating:
            self._generating = False
            self._gen_token += 1  # обезсилва резултата на изоставената нишка, ако/когато дойде
            self._stop_spinner()
            log = self.query_one("#log", RichLog)
            log.write(
                "[bold yellow]⚠ Прекъснато с Ctrl+C. Фоновото извикване може да довърши тихо в "
                "нишката, без ефект — вече написаните файлове остават на диска. Натисни Ctrl+C "
                "отново на празен вход, за да излезеш.[/]"
            )
            input_widget = self.query_one("#brief", Input)
            input_widget.disabled = False
            input_widget.focus()
        else:
            self.exit()


def main() -> int:
    from src.terminal import force_utf8_io

    force_utf8_io()
    AgentTUI().run()

    # Форсиран изход веднага след AgentTUI.run() (терминалът вече е възстановен вътре в
    # него, преди run() да върне контрола — виж App._shutdown()/driver.close()). Без това
    # Ctrl+C по време на активна генерация (LLM извикването тече във фонова нишка на
    # run_worker(thread=True), т.е. в стандартния concurrent.futures пул на asyncio) изглежда
    # все едно не работи: приложението вече е затворено, но Python чака нишката да приключи
    # при нормално спиране (atexit._python_exit джойва всички такива нишки) — с реален
    # LLM извикване това са секунди до минути. os._exit() пропуска тази стъпка изцяло,
    # също както Ctrl+C в src/repl.py прекъсва веднага, без да чака текущия пост.
    os._exit(0)
