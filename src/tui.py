"""
Пълен full-screen TUI, върху Textual — по потребителска заявка за "премиум" изглед
(2026-08-17). Съществува ЗАЕДНО с src/repl.py (readline-стил, ред-по-ред от 2026-08-11),
не го заменя — активира се изрично с `python -m src.cli tui`. repl.py остава подразбиращият
се начин на работа (`python -m src.cli` без аргумент); нищо в него не е пипнато.

За кратко (2026-08-17 до 2026-08-25) това беше обърнато — TUI-то стана подразбиращо се.
Върнато обратно на 2026-08-25 по потребителска инструкция: full-screen приложение задължително
грабва alt-screen буфера на терминала, а докато той е грабнат, никой terminal emulator не дава
собствен (native) scrollback върху него — фундаментално ограничение на терминалите, не бъг тук
за поправяне. Claude Code CLI (и repl.py) НЕ ползват alt-screen въобще, затова там scrollback
работи изцяло нормално. TUI-то остава напълно работещо и се пуска изрично с `tui`, за който
предпочита "премиум" изгледа му пред native scrollback-а.

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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static, TextArea

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.config import Config, ConfigError, load_config
from src.errors import AgentError
from src.llm.client import LLMClient
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline import modes
from src.pipeline.preview import render_preview
from src.pipeline.runner import FinalCheckUpdate, run_pipeline
from src.pipeline.util import file_uri, read_text_or_none
from src.repl import (
    _STEP_ORDER,
    _STEP_TAG_RE,
    _default_file_picker,
    _open_in_file_manager,
    copy_attachments,
    copy_to_clipboard,
    extract_inline_attachments,
    resolve_attachments,
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


class BriefInput(TextArea):
    """
    Полето за брифа — многоредово, с пренасяне на текста (soft wrap), вместо еднолинейния
    `Input`, който преди него скролираше хоризонтално и показваше само парче от по-дългия
    бриф (потребителска обратна връзка 2026-08-25: "инпут полето да пренася текста, да не е
    на един ред").

    Enter изпраща брифа (както преди при `Input`); Ctrl+J вмъква нов ред в брифа самия —
    Shift+Enter НЕ е избираем тук нарочно: терминалите нямат стандартен начин да различат
    Shift+Enter от обикновено Enter (виж проверка на textual/keys.py — няма запис за
    "shift+enter"), докато Ctrl+J е отделен, надежден байт (LF), разпознаваем от textual.
    """

    class Submitted(Message):
        def __init__(self, brief_input: "BriefInput", value: str) -> None:
            self.brief_input = brief_input
            self.value = value
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if event.key == "ctrl+j":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)


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
        /* min-height е задължителен: #output по-долу може да поиска до 55% от екрана, а
           "1fr" без долна граница може да се свие практически до 0 в тесен terminal —
           точно това счупи скрола в лога (потребителска обратна връзка 2026-08-25:
           "не мога да скролвам в големия терминал" — нямаше го какво да скролираш,
           панелът беше свит до нищо). 6 реда остават видими и скролируеми винаги.
        */
        min-height: 6;
        border: round $primary;
        border-title-color: $secondary;
        padding: 0 1;
        margin: 0 1;
        background: $surface;
    }
    #output {
        height: auto;
        /* По-рано 82% — прекалено агресивно, задушаваше #log (виж коментара там). 55% пак
           показва дълъг пост почти изцяло без скрол за нормален terminal, но оставя реално
           място за лога отгоре. scrollbar-size-vertical остава като предпазна мрежа само за
           наистина екстремен случай (много дълга "Панорама" на много нисък terminal). */
        max-height: 55%;
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
    #output-link {
        /* ИЗВЪН скролиращия #output — статичен ред, който НЕ зависи от скрол позиция.
           Потребителска обратна връзка 2026-08-25 (два пъти): "пак не виждам линк" — линкът
           беше в #log, който скролира и може да е бил превъртян нагоре или свит. Тук е
           закачен директно под текста на поста, винаги на екрана, докато #output е видим. */
        height: auto;
        margin: 0 1 1 1;
        padding: 0 2;
        display: none;
        color: $success;
        text-style: bold;
    }
    #output-link.visible {
        display: block;
    }
    #progress {
        margin: 1 1 0 1;
    }
    #status {
        margin: 0 1;
        color: $secondary;
        text-style: italic;
    }
    #attach-row {
        height: 3;
        margin: 1 1 0 1;
        align: left middle;
    }
    #attach-button {
        min-width: 22;
        margin-right: 2;
    }
    #attach-status {
        color: $secondary;
    }
    #brief {
        margin: 1 1 1 1;
        border: round $accent;
        /* Многоредово поле, не еднолинейно — виж BriefInput по-горе за защо. 5 реда е
           достатъчно за бриф от 2-3 изречения без скрол; по-дълъг просто скролира ВЪТРЕ в
           тази малка кутия (за разлика от output-а по-горе, тук няма смисъл да расте с
           текста — брифът се пише, не се чете). */
        height: 5;
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
        file_picker_fn=None,
        open_preview_fn=None,
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
        # Резолюция при създаване, не при дефиниция: стойност по подразбиране, свързана при
        # import, заобикаляше подмяната в тестовете и отваряше истински прозорци (2026-10-12).
        self.file_picker_fn = file_picker_fn or _default_file_picker
        self.open_preview_fn = open_preview_fn or _open_in_file_manager
        self.config: Config | None = None
        self.knowledge: BrandKnowledge | None = None

        # Снимки, прикачени с бутона "📎 Прикачи снимки" (виж on_button_pressed) — чакат до
        # следващото Enter в полето за брифа, после се сливат с евентуалните @път токени в
        # самия текст (огледално на src/repl.py::SessionState.attachments) и се изчистват.
        self._attachments: list[Path] = []

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

        # Режим за дължина на следващия пост (pipeline/modes.py) — сменя се с командата
        # `/mode` в самото поле за брифа (виж on_brief_input_submitted), важи за цялата
        # сесия докато не бъде сменен пак, огледално на src/repl.py::cmd_mode.
        self.mode: str = modes.DEFAULT_MODE

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
            yield Static("", id="output-link")
            yield ProgressBar(
                id="progress",
                total=len(_STEP_ORDER),
                show_eta=False,
                # Плътен акцентен цвят, не дъга — same stop два пъти е най-простият начин да
                # накараш Gradient да рендира един-единствен цвят с наличния API.
                gradient=Gradient((0.0, _ACCENT), (1.0, _ACCENT)),
            )
            yield Static("", id="status")
            with Horizontal(id="attach-row"):
                yield Button("📎 Прикачи снимки", id="attach-button")
                yield Static("", id="attach-status")
            yield BriefInput(
                placeholder=(
                    "Опиши повода за поста (@път за снимка, /mode за дължина) и Enter… "
                    "(Ctrl+J за нов ред в брифа)"
                ),
                id="brief",
                show_line_numbers=False,
            )
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
        mode_label = modes.get_mode(self.mode).label
        log.write(
            f"[bold cyan]Готово. Напиши брифа долу и натисни Enter. "
            f"Дължина: {mode_label} (по подразбиране) — /mode за Кадър/Панорама.[/]"
        )
        self.query_one("#brief", BriefInput).focus()

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

    # ------------------------------------------------------------------ история (Ctrl+↑/Ctrl+↓)

    def on_key(self, event: events.Key) -> None:
        """
        Ctrl+↑/Ctrl+↓ в полето за брифа прелистват историята. Обикновено ↑/↓ вече не могат
        да свършат тази работа откакто брифът е многоредов (BriefInput/TextArea): те са
        нужни за движение на курсора между редовете на самия бриф (TextArea ги бинди сама —
        виж widgets/_text_area.py BINDINGS), затова историята мина на Ctrl+вариант, който
        нищо друго не ползва тук.
        """
        if event.key not in ("ctrl+up", "ctrl+down"):
            return
        input_widget = self.query_one("#brief", BriefInput)
        if not input_widget.has_focus or input_widget.disabled or not self._history:
            return

        if event.key == "ctrl+up":
            if self._history_index == 0:
                return
            if self._history_index == len(self._history):
                self._history_draft = input_widget.text
            self._history_index -= 1
        else:
            if self._history_index >= len(self._history):
                return
            self._history_index += 1

        input_widget.text = (
            self._history_draft
            if self._history_index == len(self._history)
            else self._history[self._history_index]
        )
        input_widget.move_cursor(input_widget.document.end)
        event.prevent_default()
        event.stop()

    # ------------------------------------------------------------------ генериране

    def on_brief_input_submitted(self, event: BriefInput.Submitted) -> None:
        raw = event.value.strip()
        if not raw or self.config is None:
            return

        # Запис в историята преди всичко останало — независимо дали пускането ще успее,
        # потребителят трябва да може да го извика обратно с Ctrl+↑ (виж on_key).
        if not self._history or self._history[-1] != raw:
            self._history.append(raw)
        self._history_index = len(self._history)
        self._history_draft = ""

        # `/mode ...` е команда, не бриф — сменя дължината на СЛЕДВАЩИЯ пост (огледално на
        # src/repl.py::cmd_mode), не тръгва конвейер. Разпознава се преди @-извличането,
        # за да не се опитва то да чете снимки от текст като "/mode панорама".
        if raw.startswith("/"):
            name, _, argument = raw[1:].partition(" ")
            if name.strip().lower() == "mode":
                self._handle_mode_command(argument.strip())
                event.brief_input.text = ""
                return
            self.query_one("#log", RichLog).write(
                f"[bold red]Непозната команда '/{name}'. Единствената тук е /mode.[/]"
            )
            return

        brief, inline_photos, unresolved = extract_inline_attachments(raw)
        if not brief:
            self.query_one("#log", RichLog).write("[bold red]Брифът е празен.[/]")
            return

        input_widget = self.query_one("#brief", BriefInput)
        input_widget.disabled = True
        input_widget.text = ""

        # Снимки от бутона "📎" + снимки от @токени в реда — обединени, огледално на
        # src/repl.py::_new_post. Изчистват се тук, не се пренасят към следващия пост.
        photos = self._attachments + inline_photos
        self._attachments = []
        self._update_attach_status()

        post_dir = unique_post_dir(self.posts_dir, slugify(brief), today=date.today())
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")
        copied = copy_attachments(photos, post_dir / "photos")

        log = self.query_one("#log", RichLog)
        output = self.query_one("#output", VerticalScroll)
        output.remove_class("visible")
        self.query_one("#output-link", Static).remove_class("visible")
        bar = self.query_one("#progress", ProgressBar)
        bar.update(progress=0)
        mode_obj = modes.get_mode(self.mode)
        log.write(
            f"[bold]▸ Нов пост: {post_dir}[/] · режим: {mode_obj.label}"
            + (f" · {len(copied)} снимка(и)" if copied else "")
        )
        for item in unresolved:
            log.write(f"[dim](@{item} не е съществуваща снимка — остава като текст в брифа)[/]")

        self._current_step_desc = "стартиране…"
        self._generating = True
        self._gen_token += 1
        token = self._gen_token
        mode_for_run = self.mode  # снимка на СЕГАШНИЯ избор — /mode по-късно не бива да го променя in-flight
        self._start_spinner()
        self.run_worker(
            lambda: self._generate(post_dir, token, mode_for_run), thread=True, exclusive=True, group="generate"
        )

    # ------------------------------------------------------------------ прикачени снимки (бутон)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attach-button":
            self._pick_photos()

    def _pick_photos(self) -> None:
        """
        Отваря системния диалог за избор на файлове — истинска кликаема икона с мишка,
        за разлика от текстовия "+" еквивалент в src/repl.py (потребителска заявка:
        "искам да е там като иконка и при натискане да прикачвам снимки", 2026-09-02).

        Worker thread, не директно на главния event loop: tkinter.filedialog е блокираща
        GUI извикване (виж `_default_file_picker` в repl.py) — точно както LLM извикването
        в `_generate` по-долу, не бива да спира рендирането на самия TUI, докато диалогът е
        отворен.
        """
        if self._generating:
            return  # избягва конфликт с активна генерация, огледално на другите действия
        self.query_one("#attach-button", Button).disabled = True
        self.run_worker(self._pick_photos_worker, thread=True, exclusive=True, group="pick-photos")

    def _pick_photos_worker(self) -> None:
        paths = self.file_picker_fn()
        self.call_from_thread(self._on_photos_picked, paths)

    def _on_photos_picked(self, paths) -> None:
        self.query_one("#attach-button", Button).disabled = False
        log = self.query_one("#log", RichLog)
        if paths is None:
            log.write("[bold red]Диалог за избор на файлове не е наличен в тази среда.[/]")
            return
        if not paths:
            return  # потребителят е затворил диалога без избор — тихо, без бележка
        found, unresolved = resolve_attachments(paths)
        self._attachments.extend(found)
        for item in unresolved:
            log.write(f"[bold red]✗ не разпознат файл: {item}[/]")
        self._update_attach_status()

    def _update_attach_status(self) -> None:
        status = self.query_one("#attach-status", Static)
        count = len(self._attachments)
        status.update(f"{count} снимка(и) прикачени" if count else "")

    def _handle_mode_command(self, argument: str) -> None:
        """`/mode` в полето за брифа — виж докстринга на on_brief_input_submitted."""
        log = self.query_one("#log", RichLog)
        if not argument:
            current = modes.get_mode(self.mode)
            log.write(f"[bold]Текущ режим: {current.label}[/]")
            log.write(f"[dim]Налични: {modes.mode_list_text()}[/]")
            log.write("[dim]Смяна: /mode кадър · /mode фокус · /mode панорама[/]")
            return
        key = modes.resolve_mode(argument)
        if key is None:
            log.write(f"[bold red]Непознат режим '{argument}'. Налични: {modes.mode_list_text()}[/]")
            return
        self.mode = key
        chosen = modes.get_mode(key)
        log.write(f"[bold green]✓ Режим: {chosen.label}[/] — важи от следващия пост нататък.")

    def _generate(self, post_dir: Path, token: int, mode: str) -> None:
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

        def on_final_check(update: FinalCheckUpdate) -> None:
            # Извикано от СЪВСЕМ друг daemon thread (стъпка [7], виж runner.py), често дълго
            # след като този _generate вече е приключил — НЕ се гейтва по `token` като
            # _show_result/_show_error по-долу, защото точно тогава е полезно: потребителят
            # може вече да гледа друг пост, докато закъснялата бележка идва за този тук.
            self.call_from_thread(self._show_final_check_update, update)

        try:
            result = self.pipeline_fn(
                post_dir, client, knowledge=self.knowledge, mode=mode, on_final_check=on_final_check
            )
        except AgentError as exc:
            self.call_from_thread(self._show_error, post_dir, exc, token)
        else:
            self.call_from_thread(self._show_result, result, token, mode)
        finally:
            unregister_persistent_handler(handler)
            self.call_from_thread(self._reenable_input, token)

    def _show_result(self, result, token: int, mode: str) -> None:
        if token != self._gen_token:
            return  # прекъснато с Ctrl+C междувременно — резултатът дойде закъснял, игнорира се
        text = (read_text_or_none(result.post_path) or "").strip()
        output = self.query_one("#output", VerticalScroll)
        self.query_one("#output-text", Static).update(text)
        output.border_title = "Готов текст"
        output.add_class("visible")
        output.scroll_home(animate=False)  # нов резултат винаги се показва от началото

        log = self.query_one("#log", RichLog)

        if result.resolution_summary:
            log.write(f"[dim]{result.resolution_summary}[/]")
        for fix in result.applied_fixes:
            log.write(f"[green]✎ {fix}[/]")
        for note in result.open_notes:
            log.write(f"[yellow]⚠ {note}[/]")
        for warning in result.warnings:
            log.write(f"[yellow]⚠ {warning}[/]")

        status = "[bold green]✓ ready[/]" if result.status == "ready" else "[bold yellow]⚠ ready_with_notes[/]"
        mode_label = modes.get_mode(mode).label
        log.write(f"{status} [dim]{mode_label} · {len(text.split())} думи · файл: {file_uri(result.post_path)}[/]")

        copied = bool(text) and copy_to_clipboard(text)
        if copied:
            log.write("[dim]📋 копирано в клипборда[/]")

        # Линкът/копирането излизат ТУК, в отделен статичен ред извън #log — потребителска
        # обратна връзка 2026-08-25 (два пъти): "пак не виждам линк". #log скролира и/или
        # може да е свит (виж CSS коментара при #log за защо се свиваше); ред, написан само
        # там, лесно остава невидим — извън екрана нагоре или под свит панел. #output-link е
        # закачен директно под текста на поста и НЕ скролира — винаги на екрана, докато
        # изходът е видим, независимо какво е състоянието на лога отгоре.
        link_line = self.query_one("#output-link", Static)
        try:
            preview_path = render_preview(result.output_dir.parent)
            copy_note = " · 📋 копирано в клипборда" if copied else ""
            link_line.update(f"🔗 Отвори в браузър (реалистичен изглед): {file_uri(preview_path)}{copy_note}")
            link_line.add_class("visible")
            # Автоматично отваряне (потребителска обратна връзка 2026-09-03: "не се отваря
            # директно в браузъра") — отделен OS прозорец, не пипа alt-screen буфера на TUI-то.
            try:
                self.open_preview_fn(preview_path)
            except Exception:
                pass
        except (FileNotFoundError, OSError) as exc:
            log.write(f"[dim]⚠ Прегледът не се генерира: {exc}[/]")
            link_line.update(f"⚠ Прегледът не се генерира: {exc}")
            link_line.add_class("visible")

    def _show_final_check_update(self, update: FinalCheckUpdate) -> None:
        """
        Закъсняла нотификация от стъпка [7] (виж on_final_check в _generate) — идва от фонов
        thread, потенциално минути след като потребителят вече е видял "✓ ready" и е започнал
        нов пост. Нарочно винаги се показва (не се гейтва по `_gen_token`), с изрична поява на
        коя папка се отнася — известното, документирано ограничение (TUI/REPL не се обновяват
        визуално при закъсняла находка) вече има видим изход, не само запис в meta.json.
        """
        log = self.query_one("#log", RichLog)
        label = update.post_dir.name
        if update.status == "failed":
            log.write(f"[yellow]⚠ (async: {label}) финалната проверка не успя: {update.error}[/]")
            return
        if not update.notes:
            log.write(f"[dim]✓ (async: {label}) финалната проверка на предложението завърши чисто.[/]")
            return
        log.write(f"[bold yellow]⚠ (async: {label}) финалната проверка намери {len(update.notes)} нови бележки:[/]")
        for note in update.notes:
            log.write(f"[yellow]  · {note}[/]")

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
        input_widget = self.query_one("#brief", BriefInput)
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
