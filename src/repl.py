"""
Интерактивен терминален режим — `python -m src.cli` (или `linkedin-agent`).

Човек пише бриф на промпта, агентът минава през конвейера (Планер → Писател +
Media Picker → Критик → Ревизия → Консолидатор) и отпечатва готовия пост. После
може да го дошлайфа с /revise, без да пише брифа наново.

Разликата с `generate` не е само удобство:

  - Не се налага да мислиш за папки. Всеки промпт си създава папка `posts/<дата>-<slug>`
    сам, така че одитната следа на диска остава същата, каквато я иска остатъкът от
    системата — но човекът не я въвежда ръчно.
  - `knowledge/` се чете ЕДНОКРАТНО при старт и се преизползва през всички промпти
    (при `generate` се чете наново за всяко пускане). /reload го препрочита, когато
    някой е пипал knowledge/ междувременно.
  - Прогресът се вижда докато върви. Стъпките вече се логват в run.log — тук същият
    logger се препраща и към конзолата през persistent handler (виж
    src/logging_utils.py), защото конвейер от 5–7 модела отнема минути и мълчалив
    терминал изглежда като забил.

Нарочно НЕ се преизползва един LLMClient между промптите: неговият call_log влиза в
output/meta.json, и споделен клиент би записал в meta.json на втория пост извикванията
на първия. Един пост — един клиент — една чиста одитна следа.

От 2026-08-12 („Ниво 0" на предложението за по-лесна употреба от нетехнически човек):

  - Снимки — провлачен път в терминала се приема директно, без нужда от `/attach`
    или `@`, на въпроса за снимки в `/wizard`.
  - Готовият текст се копира автоматично в клипборда след всяко успешно генериране —
    вместо ръчно маркиране в терминала. Best-effort: провал в копирането е тих (виж
    copy_to_clipboard), никога не чупи сесията.
  - Ако `.env` липсва изцяло, при старт се предлага еднократно създаване (само с
    OPENWEBUI_API_KEY, останалото от `.env.example`) вместо изискване за ръчно
    редактиране на файл, преди сесията изобщо да тръгне.

От 2026-08-12, втора итерация (потребителска обратна връзка — първата версия на
/wizard разпитваше за повод/причина/кого-да-спомене/чувствителност/бележки едно по
едно и на практика не се различаваше от обикновен чат промпт с формуляр отгоре):

  - `/wizard` вече пита само за снимки, и то само ако вече няма нито една — всичко
    друго конвейерът вече решава сам (Планерът преценява чувствителност от текста,
    Писателят вече отказва да измисля имена и минава на безопасно общо описание,
    когато никой член няма потвърдено съгласие — виж render_sources_block в
    pipeline/writer.py). Разпит преди всеки пост пазеше от риск, който кодът вече
    пази сам; питането оставаше само инерция.
  - Банерът при старт показва реални числа от заредения корпус (членове, еталонни
    постове), не само име — за да е ясно от първия ред, че сесията не е празен чат,
    а зареден с контекст за конкретна организация.

От 2026-08-12, трета итерация (потребителска заявка — "нов" колкото ключова дума за
отваряне на уизарда, и по-подробни, подредени логове, не само номер на стъпка):

  - Бара дума `нов` (или `wizard`, без слаш) отваря /wizard — не изисква запомняне на
    команден синтаксис (виж _WIZARD_KEYWORDS). `/wizard`/`/w` остават като преди.
  - `/wizard` вече показва кратко удебелено заглавие ("▸ Нов пост") при отваряне и
    потвърждение ("Разбрах — тръгвам.") преди да подаде брифа на конвейера.
  - ProgressHandler вече групира логовете по стъпка вместо плосък поток: нов таг
    ("[4]", "[6б]"...) отваря удебелена секция, всичко докладвано вътре в нея (план,
    находки, избрани снимки, поправки, открити въпроси) виси отдолу с отстъп. Самите
    runner.py логове носят повече съдържание — не само "Критик готово: 3 находки", а
    и самите три находки, ред по ред (виж src/pipeline/runner.py).
"""

from __future__ import annotations

import glob as globlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.text import Text

from src.brand.loader import BrandKnowledge, audit_knowledge, load_brand_knowledge
from src.config import Config, ConfigError, load_config
from src.errors import AgentError
from src.llm.client import LLMClient
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline.consolidator import PipelineResult
from src.pipeline.runner import run_pipeline
from src.pipeline.util import PHOTO_EXTENSIONS, list_photos, read_text_or_none
from src.terminal import force_utf8_io, sanitize_input

POSTS_DIR = Path("posts")
ENV_PATH = Path(".env")
ENV_EXAMPLE_PATH = Path(".env.example")
PROMPT = "› "
CONTINUATION_PROMPT = "  "

# Кратки, естествени думи, които отварят /wizard без слаш — за човек, който не се сеща
# за командния синтаксис. И двете са безсмислени като самостоятелен бриф за пост, затова
# еднозначно се разпознават като команда, не като текст за писане.
_WIZARD_KEYWORDS = {"нов", "wizard"}

HELP_TEXT = """\
Пиши брифа на един ред и натисни Enter — агентът генерира поста. Нищо друго не пита:
кого да спомене безопасно, чувствителен ли е постът, какъв тон да ползва — това си е
негова работа, не твоя.
Ред, завършващ с \\, продължава на следващия (за по-дълъг бриф).
Снимка се прикачва с @път вътре в брифа, с /attach, или просто я провлачи при /wizard.

  нов               (или /wizard, /w) — като обикновен ред, но пита за снимки при нужда
  /attach <път> …   прикачва снимка, папка със снимки или маска (*.jpg)
  /photos           показва прикаченото; /photos clear го изчиства
  /revise <текст>   пренаписва последния пост с допълнителна инструкция
  /again            пуска последния бриф наново (всички стъпки от нула)
  /preview          отваря визуален изглед (текст + избрани снимки) в браузър
  /last             показва последния пост още веднъж
  /log [n]          последните n реда от run.log на последния пост (по подр. 30)
  /dir              папката на последния пост; /open я отваря
  /check            проверява Open WebUI и всеки конфигуриран модел
  /knowledge        какво липсва в knowledge/
  /reload           препрочита knowledge/ и .env
  /clear            изчиства екрана
  /help             това
  /exit             изход (или Ctrl+D)\
"""


# --------------------------------------------------------------------------- цветове

# force_terminal=True: решението "има ли изобщо цвят" е взето от colors_enabled() при
# конструирането на Ink — веднъж влязъл тук, текстът трябва да се оцвети безусловно.
# width е нарочно огромна: стиловете тук са единични думи/фрази с no_wrap=True, никога
# цял абзац — не искаме rich да пренася ред по средата на къс статус етикет.
_STYLE_CONSOLE = Console(force_terminal=True, color_system="truecolor", width=4096, highlight=False)


def _panel_console() -> Console:
    """Отделна конзола за panel(), с реалната ширина на терминала (в разумни граници)."""
    width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    return Console(force_terminal=True, color_system="truecolor", width=width, highlight=False)


@dataclass(frozen=True)
class Ink:
    """
    Стилов слой върху `rich` — 8-цветен ANSI (както преди) изглежда изпран на всеки
    съвременен терминал; rich носи истински truecolor и коректна поддръжка на Windows
    без ръчна работа. При `enabled=False` всеки метод връща текста НЕПОКЪТНАТ — това е
    контрактът, на който стъпват тестовете (NO_COLOR, не-tty изход, `Ink(enabled=False)`).

    Безопасно за произволно съдържание (пост от модел, бриф, съобщение за грешка):
    `rich.text.Text` НЕ парсва markup — низ като „[линк]" минава дословно, за разлика от
    `console.print("[линк]")`, което rich би се опитало да разчете като таг.
    """

    enabled: bool = True

    def _wrap(self, text: str, style: str) -> str:
        if not self.enabled:
            return text
        with _STYLE_CONSOLE.capture() as capture:
            _STYLE_CONSOLE.print(Text(text, style=style, no_wrap=True), end="")
        return capture.get()

    def dim(self, text: str) -> str:
        return self._wrap(text, "grey62")

    def bold(self, text: str) -> str:
        return self._wrap(text, "bold")

    def ok(self, text: str) -> str:
        return self._wrap(text, "bold green3")

    def warn(self, text: str) -> str:
        return self._wrap(text, "bold orange3")

    def err(self, text: str) -> str:
        return self._wrap(text, "bold red3")

    def accent(self, text: str) -> str:
        return self._wrap(text, "bold cyan3")

    def panel(self, body: str, *, title: str = "", border_style: str = "cyan3") -> str:
        """
        Рамка около важен блок текст (готовия пост, банера) — центърът на визуалната
        разлика спрямо обикновен ред в терминала. При `enabled=False` пада обратно към
        обикновена хоризонтална черта, за да не се чупят тестове/NO_COLOR/не-tty изход,
        писани срещу плоския текстов формат.
        """
        if not self.enabled:
            rule = "─" * 60
            header = f"{title}\n" if title else ""
            return f"{header}{rule}\n{body}\n{rule}"
        console = _panel_console()
        with console.capture() as capture:
            console.print(
                Panel(Text(body, style="none"), title=title or None, border_style=border_style, expand=True)
            )
        return capture.get().rstrip("\n")


def colors_enabled(stream=None) -> bool:
    """NO_COLOR (de-facto стандарт) и не-tty изход изключват цветовете."""
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


# --------------------------------------------------------------------------- имена на папки

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sht", "ъ": "a", "ь": "y", "ю": "yu", "я": "ya",
}


def slugify(text: str, *, max_words: int = 5, max_length: int = 42) -> str:
    """
    Прави от бриф на кирилица име на папка на латиница. Имената на папките се пишат в
    пътища, логове и meta.json — латиница ги прави безболезнени за копиране, git и shell.
    """
    latin = "".join(_TRANSLIT.get(char, char) for char in text.lower())  # транслитерация буква по буква
    words = re.findall(r"[a-z0-9]+", latin)  # маха пунктуация/интервали, взима само думи
    slug = "-".join(words[:max_words])[:max_length].strip("-")
    return slug or "post"  # изцяло нетранслитерируем текст (само емоджи/символи) → "post"


def unique_post_dir(posts_dir: Path, slug: str, *, today: date | None = None) -> Path:
    """posts/<дата>-<slug>, с наставка -2, -3 … ако вече е зает."""
    stamp = (today or date.today()).isoformat()
    base = f"{stamp}-{slug}"
    candidate = posts_dir / base
    counter = 2
    while candidate.exists():  # втори пост същия ден със същия/подобен бриф → -2, -3, ...
        candidate = posts_dir / f"{base}-{counter}"
        counter += 1
    return candidate


# --------------------------------------------------------------------------- прикачени снимки

_ATTACHMENT_TOKEN = re.compile(r'@"([^"]+)"|@(\S+)')


def _expand_one(raw: str) -> list[Path]:
    """Един аргумент → списък снимки. Приема файл, папка или маска (glob)."""
    cleaned = raw.strip().strip('"').strip("'")
    candidate = Path(cleaned).expanduser()
    if candidate.is_dir():
        return list_photos(candidate)
    if candidate.is_file():
        return [candidate] if candidate.suffix.lower() in PHOTO_EXTENSIONS else []
    # globlib, не Path.glob — Path.glob не приема абсолютна маска (C:\...\*.jpg)
    expanded = os.path.expanduser(cleaned)
    return sorted(
        Path(match) for match in globlib.glob(expanded) if Path(match).suffix.lower() in PHOTO_EXTENSIONS
    )


_QUOTED_OR_BARE_TOKEN = re.compile(r'"([^"]+)"|(\S+)')


def split_path_args(text: str) -> list[str]:
    """
    Разбива една реплика на отделни пътища — поддържа "път с интервали" в кавички.
    Споделено между /attach и /wizard (стъпката за снимки), за да приемат едно и също.
    """
    return [quoted or bare for quoted, bare in _QUOTED_OR_BARE_TOKEN.findall(text)]


def resolve_attachments(args: Iterable[str]) -> tuple[list[Path], list[str]]:
    """Връща (намерени снимки, ненамерени/неразпознати аргументи)."""
    found: list[Path] = []
    unresolved: list[str] = []
    for raw in args:
        if not raw:
            continue
        matches = _expand_one(raw)
        if matches:
            found.extend(matches)
        else:
            unresolved.append(raw)
    return found, unresolved


def extract_inline_attachments(text: str) -> tuple[str, list[Path], list[str]]:
    """
    Изважда `@път` от текста на брифа. Токен, който НЕ сочи към съществуваща снимка,
    остава в текста непокътнат — @споменаване в бриф е нормален български текст и не
    бива да се яде тихо.
    """
    found: list[Path] = []
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1) or match.group(2)
        matches = _expand_one(raw)
        if matches:
            found.extend(matches)
            return ""
        unresolved.append(raw)
        return match.group(0)

    cleaned = _ATTACHMENT_TOKEN.sub(replace, text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), found, unresolved


def copy_attachments(attachments: Iterable[Path], photos_dir: Path) -> list[str]:
    """Копира прикачените снимки в photos/ на поста, без да презаписва при съвпадащо име."""
    photos_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for source in attachments:
        target = photos_dir / source.name
        counter = 2
        while target.exists():
            target = photos_dir / f"{source.stem}-{counter}{source.suffix}"
            counter += 1
        shutil.copy2(source, target)
        copied.append(target.name)
    return copied


# --------------------------------------------------------------------------- прогрес


# Редове от run.log, които интерактивният режим НЕ повтаря на конзолата. Всички са
# или чист шум за човек пред терминала, или дублират нещо, което REPL-ът и без това
# показва по-четимо след поста (резюме, предупреждения, съобщението на AgentError).
# run.log си остава пълен — тук се реже само екранът.
_SUPPRESSED_LOG_PREFIXES = (
    "===",  # разделителят "Ново пускане ..."
    "Спряно:",  # следва го human_message на AgentError, дословно същото
    "Финален статус:",  # резюмето след поста го казва по-кратко
    "Открит въпрос:",  # обобщаващ дублат в края — всеки вече е показан inline под своята стъпка (виж runner.py)
    "Папката 'photos/'",  # media_picker логва празната папка, runner я логва пак
)

# Разпознава водещия таг на стъпка в лог съобщение — "[1]", "[2]/[3]", "[6б]", "[7]".
# Ред със същия таг като предишния е ДЕТАЙЛ на текущата стъпка (показва се на отделен
# ред, с отстъп); нов таг отваря нова секция с удебелено заглавие. Ред без таг изобщо
# (напр. отделна находка, вече отстъпена от runner.py) също се третира като детайл на
# текущата отворена секция — идва хронологично веднага след нея.
_STEP_TAG_RE = re.compile(r"^(\[[^\]]+\](?:/\[[^\]]+\])?)\s*")

# Редът на таговете, точно както ги пише runner.py (виж докстринга на модула там) — за
# прогрес-бара в _run_with_spinner: позицията на тага в този списък е "стъпка N от 7".
# "[6б]" не е отделно LLM извикване (механичните правила се налагат в кода), но си остава
# видима отсечка от гледна точка на човек пред терминала, затова е в списъка.
_STEP_ORDER = ["[1]", "[2]/[3]", "[4]", "[5]", "[6]", "[6б]", "[7]"]


class ProgressHandler(logging.Handler):
    """
    Показва стъпките на конвейера докато вървят, групирани по секции — минути мълчалив
    терминал изглеждат като забил процес, а плосък поток от редове без структура е също
    толкова безполезен: не личи кое принадлежи на коя стъпка. Всяка нова стъпка отваря
    удебелено заглавие; всичко, докладвано вътре в нея (план, находки, поправки, открити
    въпроси), виси отдолу с отстъп, докато не тръгне следваща стъпка.
    """

    def __init__(self, emit_line: Callable[[str], None], ink: Ink) -> None:
        super().__init__(level=logging.INFO)
        self._emit_line = emit_line
        self._ink = ink
        self._current_tag: str | None = None
        # Задава се отвън, само докато траят на екран прогрес-бар (виж _run_with_spinner) —
        # извиква се веднъж на всяка НОВА стъпка, с (таг, пълно съобщение). None извън
        # генериране — handler-ът си стои закачен цяла сесия, барът съществува само за
        # едно пускане.
        self.on_step: Callable[[str, str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(_SUPPRESSED_LOG_PREFIXES):
            return

        match = _STEP_TAG_RE.match(message)
        tag = match.group(1) if match else None

        if tag and tag != self._current_tag:
            self._current_tag = tag
            if self.on_step:
                self.on_step(tag, message)
            self._emit_line("")
            self._emit_line(self._ink.bold(f"▸ {message}"))
            return

        detail = message[match.end():].strip() if match else message
        is_warning = record.levelno >= logging.WARNING
        bullet = "⚠" if is_warning else "·"
        wrap = self._ink.warn if is_warning else self._ink.dim
        self._emit_line(wrap(f"    {bullet} {detail}"))


# --------------------------------------------------------------------------- състояние


@dataclass
class SessionState:
    """Каквото Repl помни между промптите в една терминална сесия — не преживява рестарт."""

    attachments: list[Path] = field(default_factory=list)
    post_dir: Path | None = None
    brief: str = ""
    revisions: int = 0


# --------------------------------------------------------------------------- REPL


class Repl:
    """Интерактивната сесия — виж докстринга на модула. Всички зависимости (модел, конфигурация,
    knowledge loader, pipeline функция) са инжектирани през __init__ по подразбиране към реалните
    им имплементации, за да могат тестовете да ги подменят без мрежа/диск."""

    def __init__(
        self,
        *,
        posts_dir: Path = POSTS_DIR,
        out: Callable[[str], None] | None = None,
        input_fn: Callable[[str], str] = input,
        ink: Ink | None = None,
        config_loader: Callable[[], Config] = load_config,
        knowledge_loader: Callable[[], BrandKnowledge] = load_brand_knowledge,
        client_factory: Callable[[Config], LLMClient] = LLMClient,
        pipeline_fn: Callable[..., PipelineResult] = run_pipeline,
        clipboard_fn: Callable[[str], bool] | None = None,
        env_path: Path = ENV_PATH,
        env_example_path: Path = ENV_EXAMPLE_PATH,
        today: date | None = None,
    ) -> None:
        self.posts_dir = posts_dir
        self.out = out or (lambda line: print(line))
        self.input_fn = input_fn
        self.ink = ink or Ink(colors_enabled())
        self.config_loader = config_loader
        self.knowledge_loader = knowledge_loader
        self.client_factory = client_factory
        self.pipeline_fn = pipeline_fn
        self.clipboard_fn = clipboard_fn or copy_to_clipboard
        self.env_path = env_path
        self.env_example_path = env_example_path
        self.today = today

        self.config: Config | None = None
        self.knowledge: BrandKnowledge | None = None
        self.state = SessionState()
        self._running = True
        self._progress_handler: ProgressHandler | None = None

    # ------------------------------------------------------------------ цикъл

    def run(self) -> int:
        # Първо пускане без .env: предложи еднократно създаване, вместо да изискваш
        # ръчно редактиране на файл, преди сесията изобщо да е започнала.
        if not self.env_path.exists() and self.env_example_path.exists():
            self._offer_env_setup()

        # Конфигурация и корпус се зареждат ЕДНОКРАТНО тук — не при всеки промпт (виж
        # докстринга на модула защо това е различно от `generate`).
        try:
            self.config = self.config_loader()
        except ConfigError as exc:
            self.out(self.ink.err(f"Грешка в конфигурацията: {exc}"))
            return 1

        self.knowledge = self.knowledge_loader()
        self._banner()

        # ProgressHandler препраща логовете на конвейера към конзолата в реално време,
        # докато е закачен — виж logging_utils.register_persistent_handler.
        handler = ProgressHandler(self.out, self.ink)
        self._progress_handler = handler
        register_persistent_handler(handler)
        try:
            while self._running:
                try:
                    line = self._read_input()
                except (EOFError, KeyboardInterrupt):
                    self.out("")
                    break
                if line is None or not line.strip():
                    continue
                try:
                    self.handle(line.strip())
                except KeyboardInterrupt:
                    # Ctrl+C по време на конвейер прекъсва само текущия пост. Всичко
                    # написано дотук остава на диска — следващ /again продължава оттам.
                    self.out(self.ink.warn("\n  ⚠ Прекъснато. Написаните файлове остават на диска."))
                except Exception as exc:  # нарочно широко — един счупен пост не бива да
                    # сваля цялата сесия. AgentError вече е обработена по-надолу с човешко
                    # съобщение; тук остават само неочакваните (счупен път, права, бъг).
                    self.out(self.ink.err(f"  ✗ Неочаквана грешка: {type(exc).__name__}: {exc}"))
        finally:
            unregister_persistent_handler(handler)

        self.out(self.ink.dim("Дочуване."))
        return 0

    def _read_input(self) -> str | None:
        """Чете един логически ред; ред, завършващ с `\\`, продължава на следващия."""
        line = self.input_fn(self.ink.accent(PROMPT))
        parts = [line]
        while parts[-1].rstrip().endswith("\\"):
            parts[-1] = parts[-1].rstrip()[:-1].rstrip()
            parts.append(self.input_fn(self.ink.dim(CONTINUATION_PROMPT)))
        return sanitize_input("\n".join(part for part in parts).strip())

    def _offer_env_setup(self) -> None:
        """
        Пита еднократно за OPENWEBUI_API_KEY и записва .env от .env.example — за да не се
        изисква от нетехнически човек да отваря и редактира файл, преди сесията да тръгне.
        Останалите настройки (модели, URL) остават каквито са в .env.example; който иска
        друг модел, си остава свободен да редактира .env ръчно после.
        """
        answer = self.input_fn(self.ink.accent("Няма .env — да го създам ли сега? (Enter = да, 'не' за отказ) › "))
        if answer.strip().lower() in {"не", "n", "no"}:
            self.out(self.ink.dim("  Пропуснато — създай .env ръчно от .env.example, преди да продължиш."))
            return

        api_key = self.input_fn(self.ink.accent("  OPENWEBUI_API_KEY › ")).strip()
        template = self.env_example_path.read_text(encoding="utf-8")
        filled = re.sub(r"^OPENWEBUI_API_KEY=.*$", f"OPENWEBUI_API_KEY={api_key}", template, count=1, flags=re.MULTILINE)
        self.env_path.write_text(filled, encoding="utf-8")
        self.out(self.ink.ok(f"  ✓ Записан {self.env_path} — редактирайте го за друг модел/URL при нужда."))

    def _banner(self) -> None:
        """
        Показва какво агентът вече знае, преди първия въпрос — за да е ясно от първия
        ред, че това не е празен ChatGPT прозорец, а зареден с корпуса на конкретна
        организация. Без реални числа тук банерът е самò име и щеше да изглежда еднакво
        с генеричен чат, независимо колко „умен" е самият конвейер отдолу.
        """
        assert self.config is not None
        assert self.knowledge is not None

        org_name = "ИКТ Клъстер Варна"
        if self.knowledge.facts:
            org_name = self.knowledge.facts.get("organization", {}).get("name_bg") or org_name

        body = "\n".join(
            [
                f"Заредено: {len(self.knowledge.members)} членове · "
                f"{len(self.knowledge.good_examples)} еталонни поста · "
                f"писател: {self.config.model_writer}",
                "",
                "Напиши едно изречение за какво е постът — останалото го решавам сам.",
                "Напиши 'нов', ако искаш помощ конкретно със снимки. /help за всички команди.",
            ]
        )
        self.out(self.ink.panel(body, title=f"LinkedIn агент — {org_name}"))
        self.out("")

    # ------------------------------------------------------------------ команди

    def handle(self, line: str) -> None:
        """Разпределя един въведен ред: обикновен текст → нов пост; `/име ...` → команда."""
        # Кратка ключова дума отваря /wizard без нужда от запомняне на слаш-синтаксис —
        # "нов" не е разумен САМОСТОЯТЕЛЕН бриф, затова еднозначно е команда, не текст.
        if line.strip().lower() in _WIZARD_KEYWORDS:
            self.cmd_wizard("")
            return

        if not line.startswith("/"):
            self._new_post(line)
            return

        name, _, argument = line[1:].partition(" ")
        argument = argument.strip()
        commands: dict[str, Callable[[str], None]] = {
            "help": self.cmd_help,
            "?": self.cmd_help,
            "wizard": self.cmd_wizard,
            "w": self.cmd_wizard,
            "attach": self.cmd_attach,
            "preview": self.cmd_preview,
            "photos": self.cmd_photos,
            "revise": self.cmd_revise,
            "again": self.cmd_again,
            "regen": self.cmd_again,
            "last": self.cmd_last,
            "log": self.cmd_log,
            "dir": self.cmd_dir,
            "open": self.cmd_open,
            "check": self.cmd_check,
            "knowledge": self.cmd_knowledge,
            "reload": self.cmd_reload,
            "clear": self.cmd_clear,
            "exit": self.cmd_exit,
            "quit": self.cmd_exit,
            "q": self.cmd_exit,
        }
        command = commands.get(name.lower())
        if command is None:
            self.out(self.ink.err(f"Непозната команда '/{name}'. /help показва наличните."))
            return
        command(argument)

    def cmd_help(self, _argument: str = "") -> None:
        self.out(HELP_TEXT)

    def cmd_exit(self, _argument: str = "") -> None:
        self._running = False

    def cmd_clear(self, _argument: str = "") -> None:
        self.out("\033[2J\033[H" if self.ink.enabled else "")

    def cmd_attach(self, argument: str) -> None:
        if not argument:
            self.out(self.ink.err("Употреба: /attach <път до снимка, папка или маска>"))
            return
        found, unresolved = resolve_attachments(split_path_args(argument))
        self.state.attachments.extend(found)
        if found:
            self.out(self.ink.ok(f"  + {len(found)} снимка(и); общо прикачени: {len(self.state.attachments)}"))
        for item in unresolved:
            self.out(self.ink.err(f"  ✗ не намерена снимка: {item}"))

    def cmd_photos(self, argument: str) -> None:
        if argument.strip().lower() in {"clear", "изчисти"}:
            self.state.attachments.clear()
            self.out(self.ink.dim("  Прикачените снимки са изчистени."))
            return
        if not self.state.attachments:
            self.out(self.ink.dim("  Няма прикачени снимки. /attach <път> или @път вътре в брифа."))
            return
        for photo in self.state.attachments:
            self.out(self.ink.dim(f"  · {photo}"))

    def cmd_dir(self, _argument: str = "") -> None:
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма генериран пост в тази сесия."))
            return
        self.out(self.ink.dim(f"  {self.state.post_dir}"))

    def cmd_open(self, _argument: str = "") -> None:
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма генериран пост в тази сесия."))
            return
        target = self.state.post_dir / "output"
        if not target.exists():
            target = self.state.post_dir
        _open_in_file_manager(target)
        self.out(self.ink.dim(f"  Отворено: {target}"))

    def cmd_last(self, _argument: str = "") -> None:
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма генериран пост в тази сесия."))
            return
        text = read_text_or_none(self.state.post_dir / "output" / "post.md")
        if text is None:
            self.out(self.ink.dim("  Няма output/post.md — последното пускане не стигна до края."))
            return
        self._print_post(text)

    def cmd_log(self, argument: str) -> None:
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма генериран пост в тази сесия."))
            return
        try:
            count = int(argument) if argument else 30
        except ValueError:
            self.out(self.ink.err("Употреба: /log [брой редове]"))
            return
        text = read_text_or_none(self.state.post_dir / "run.log")
        if text is None:
            self.out(self.ink.dim("  Няма run.log."))
            return
        for log_line in text.splitlines()[-count:]:
            self.out(self.ink.dim(f"  {log_line}"))

    def cmd_check(self, _argument: str = "") -> None:
        assert self.config is not None
        client = self.client_factory(self.config)
        self.out(self.ink.dim(f"  {self.config.openwebui_base_url}"))
        for label, info in client.check_connection().items():
            if info["ok"]:
                self.out(self.ink.ok(f"  ✓ {label:14} {info['model']:40} {info['duration_seconds']}s"))
            else:
                self.out(self.ink.err(f"  ✗ {label:14} {info['model']:40} {info.get('error', '')}"))

    def cmd_knowledge(self, _argument: str = "") -> None:
        audit = audit_knowledge()
        unusable = [entry.path for entry in audit["files"] if not entry.usable]
        for path in unusable:
            self.out(self.ink.warn(f"  ⚠ липсва или е празен: {path}"))
        if audit["facts_null_fields"]:
            self.out(self.ink.warn(f"  ⚠ непотвърдени полета във facts.yaml: {len(audit['facts_null_fields'])}"))
            for field_path in audit["facts_null_fields"]:
                self.out(self.ink.dim(f"      - {field_path}"))
        self.out(
            self.ink.dim(
                f"  {audit['members_count']} членове · {audit['good_examples_count']} добри "
                f"· {audit['bad_examples_count']} лоши примера"
            )
        )
        if not unusable and not audit["facts_null_fields"]:
            self.out(self.ink.ok("  ✓ knowledge/ е напълно попълнено."))

    def cmd_reload(self, _argument: str = "") -> None:
        try:
            self.config = self.config_loader()
        except ConfigError as exc:
            self.out(self.ink.err(f"Грешка в конфигурацията: {exc}"))
            return
        self.knowledge = self.knowledge_loader()
        self.out(self.ink.ok("  ✓ knowledge/ и .env са препрочетени."))

    def cmd_again(self, _argument: str = "") -> None:
        if self.state.post_dir is None or not self.state.brief:
            self.out(self.ink.dim("  Още няма бриф в тази сесия — напиши го на промпта."))
            return
        self._generate(self.state.post_dir, force=True)

    def cmd_revise(self, argument: str) -> None:
        """
        Ревизията НЕ е нов пост: инструкцията се дописва към briefing.md на същата папка
        и конвейерът се пуска с force. Така цялата история — оригинален бриф, всяка
        добавена бележка, всеки междинен вариант — остава в една папка, четима отгоре
        надолу. Force е задължителен: брифът се е променил, значи и планът трябва.
        """
        if not argument:
            self.out(self.ink.err("Употреба: /revise <какво да се промени>"))
            return
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма пост за ревизия — напиши бриф на промпта."))
            return

        self.state.revisions += 1
        briefing_path = self.state.post_dir / "briefing.md"
        existing = read_text_or_none(briefing_path) or ""
        addition = f"\n\n## Бележка за ревизия {self.state.revisions}\n\n{argument.strip()}\n"
        briefing_path.write_text(existing.rstrip() + addition, encoding="utf-8")
        self.state.brief = briefing_path.read_text(encoding="utf-8")
        self._generate(self.state.post_dir, force=True)

    # ------------------------------------------------------------------ генериране

    def _new_post(self, line: str) -> None:
        """Един обикновен ред от потребителя → нова папка на диска + пускане на конвейера."""
        # @път вътре в текста се изважда като снимка; неразпознат @токен си остава в брифа.
        brief, inline_photos, unresolved = extract_inline_attachments(line)
        for item in unresolved:
            self.out(self.ink.dim(f"  (@{item} не е съществуваща снимка — остава като текст в брифа)"))
        if not brief:
            self.out(self.ink.err("Брифът е празен — опиши повода за поста."))
            return

        # Снимки от /attach + снимки от @токени в реда — обединени в един списък за копиране.
        attachments = self.state.attachments + inline_photos
        self.state.attachments.clear()
        self._start_post(brief, attachments)

    def cmd_wizard(self, argument: str = "") -> None:
        """
        `/wizard <бриф>` — точно като свободен ред, без разлика в третирането на текста
        (същото @-разпознаване, същия конвейер), плюс ЕДНО нещо, което агентът наистина
        не може да познае сам: файлове по диска. Пита за снимки само ако вече няма нито
        една (нито /attach преди това, нито @токен в самия бриф) — иначе не пита нищо.

        От 2026-08-12: премахнати въпросите „защо е важно" / „кого да споменем" /
        „чувствителен ли е" / „бележки" — потребителска обратна връзка, че разпит преди
        всеки пост го прави неразличим от обикновен чат промпт. Тези решения си ги взима
        конвейерът сам: Планерът вече преценява чувствителност от самия текст на брифа;
        Писателят вече отказва да измисля имена и споменава безопасно-общо, когато никой
        член няма потвърдено съгласие (виж render_sources_block в pipeline/writer.py) —
        тоест въпросът „кого да споменем" пазеше от риск, който кодът вече пази сам.
        Ако искаш конкретно име в поста — просто го напиши в изречението, както винаги.
        """
        self.out(self.ink.bold("▸ Нов пост"))
        brief, inline_photos, unresolved = extract_inline_attachments(argument.strip())
        for item in unresolved:
            self.out(self.ink.dim(f"  (@{item} не е съществуваща снимка — остава като текст в брифа)"))

        if not brief:
            brief_raw = self.input_fn(self.ink.accent("  Какво се случи? › ")).strip()
            brief, found, unresolved = extract_inline_attachments(brief_raw)
            inline_photos.extend(found)
            for item in unresolved:
                self.out(self.ink.dim(f"  (@{item} не е съществуваща снимка — остава като текст в брифа)"))
        if not brief:
            self.out(self.ink.err("Брифът е празен — опиши повода за поста."))
            return

        attachments = list(self.state.attachments) + inline_photos
        self.state.attachments.clear()

        if not attachments:
            # Единственото, което наистина не може да се познае — има ли файлове и къде са.
            photos_raw = self.input_fn(
                self.ink.dim("  Снимки — провлачи файл(ове) тук (Enter ако няма) › ")
            ).strip()
            if photos_raw:
                found, missing = resolve_attachments(split_path_args(photos_raw))
                attachments.extend(found)
                for item in missing:
                    self.out(self.ink.err(f"  ✗ не намерена снимка: {item}"))

        self.out(self.ink.dim("  Разбрах — тръгвам."))
        self._start_post(brief, attachments)

    def _start_post(self, brief: str, attachments: list[Path]) -> None:
        """Общ край за нов пост — от свободен ред или от /wizard: папка, briefing.md, снимки, генериране."""
        post_dir = unique_post_dir(self.posts_dir, slugify(brief), today=self.today)
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")

        copied = copy_attachments(attachments, post_dir / "photos")
        self.state.post_dir = post_dir
        self.state.brief = brief
        self.state.revisions = 0

        self.out(self.ink.dim(f"  {post_dir}" + (f" · {len(copied)} снимка(и)" if copied else " · без снимки")))
        self._generate(post_dir, force=False)

    def _run_with_spinner(self, fn: Callable[[], PipelineResult]) -> PipelineResult:
        """
        Пуска `fn` във фонова нишка, докато главната показва жив прогрес-бар "стъпка N/7" +
        изтекло време — план 12, находка 3.2: 5-9 минути мълчалив терминал по време на бавна
        стъпка (напр. Media Picker с 10 снимки) не се различава от увиснал процес.

        Барът напредва в момента, в който `ProgressHandler` (виж класа по-горе) засече нов
        таг стъпка в лога — закачено през `handler.on_step`, докато този метод тече, после
        свалено. Между два тага барът стои неподвижен на текущата стъпка, само спинърът и
        часовникът се движат — честно е: не знаем колко остава ВЪТРЕ в дадена стъпка, но
        знаем кое от седемте е.

        Rich поддържа принтиране през СЪЩИЯ console, докато `Progress` е активен — барът
        коректно се спира, редът се отпечатва, барът се предрисува (документирано поведение
        на rich.progress.Progress). Затова `self.out` временно се пренасочва към
        `progress.console.print`, вместо вградения `print()`, който би счупил визуално
        живата рамка на бара.
        """
        if not self.ink.enabled:
            # NO_COLOR/не-tty/тестове — прогрес-бар без цвят е безсмислен шум, просто изчакай.
            return fn()

        box: dict[str, object] = {}

        def worker() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # включва AgentError и неочаквани — препраща ги нагоре
                box["error"] = exc

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        progress = Progress(
            SpinnerColumn(style="cyan3"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=20, complete_style="cyan3", finished_style="green3"),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=_panel_console(),
            transient=True,  # барът изчезва при край — редовете, вече отпечатани, остават
        )
        task_id = progress.add_task("Стартиране...", total=len(_STEP_ORDER))

        def on_step(tag: str, message: str) -> None:
            if tag in _STEP_ORDER:
                progress.update(task_id, completed=_STEP_ORDER.index(tag) + 1, description=message[:60])
            else:
                progress.update(task_id, description=message[:60])

        original_out = self.out
        handler = self._progress_handler
        with progress:
            self.out = lambda line: progress.console.print(line)
            if handler:
                handler.on_step = on_step
            try:
                while thread.is_alive():
                    thread.join(timeout=0.2)
            finally:
                self.out = original_out
                if handler:
                    handler.on_step = None

        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["result"]  # type: ignore[return-value]

    def _generate(self, post_dir: Path, *, force: bool) -> None:
        """Пуска конвейера върху `post_dir` и отпечатва резултата/грешката. Общ път за нов пост, /again, /revise."""
        assert self.config is not None
        # Нов клиент на всяко пускане — виж докстринга на модула защо (call_log в meta.json).
        client = self.client_factory(self.config)
        started = time.monotonic()

        try:
            result = self._run_with_spinner(
                lambda: self.pipeline_fn(post_dir, client, force=force, knowledge=self.knowledge)
            )
        except AgentError as exc:
            # Критикът вече не спира конвейера (виж runner.py), така че тук идват само
            # входни и инфраструктурни грешки. Ако въпреки това на диска има текст от
            # прекъснато пускане, показваме го — по-полезно е от чисто съобщение за грешка.
            partial = read_text_or_none(post_dir / "output" / "post.md")
            if partial:
                self._print_post(partial)
                self.out(self.ink.warn("  ⚠ Това е текст от прекъснато пускане, не финалното предложение."))
            self.out(self.ink.err(f"  ✗ Спряно: {exc.human_message}"))
            if exc.detail:
                self.out(self.ink.dim(f"  Причина: {exc.detail}"))
            self.out(self.ink.dim(f"  Пълен лог: {post_dir / 'run.log'} · /log го показва тук"))
            return

        # Успешен път: конвейерът винаги стига дотук с готово предложение (виж runner.py) —
        # печатаме текста, после резюме (статус/източници/брой извиквания/време), после
        # всичко открито за преглед.
        elapsed = time.monotonic() - started
        text = (read_text_or_none(result.post_path) or "").strip()
        self._print_post(text)

        # Копиране в клипборда вместо ръчно маркиране от терминала — best-effort, тих провал.
        if text and self.clipboard_fn(text):
            self.out(self.ink.dim("  📋 копирано в клипборда"))

        merged = " + ".join(result.merged_from) or "?"
        status = self.ink.ok("✓ предложение") if result.status == "ready" else self.ink.warn("⚠ предложение с бележки")
        calls = len(client.call_log)
        self.out(f"  {status} {self.ink.dim(f'· от {merged} · {calls} извиквания · {elapsed:.0f}s')}")
        self.out(self.ink.dim(f"  {result.post_path}"))
        if result.resolution_summary:
            self.out(self.ink.dim(f"  {result.resolution_summary}"))

        for fix in result.applied_fixes:
            self.out(self.ink.ok(f"  ✎ {fix}"))
        for note in result.open_notes:
            self.out(self.ink.warn(f"  ⚠ {note}"))
        for warning in result.warnings:
            self.out(self.ink.warn(f"  ⚠ {warning}"))
        if result.open_notes or result.warnings:
            self.out(self.ink.dim("  Бележките не спират поста — прегледай ги преди публикуване."))
        self.out(self.ink.dim("  /revise <инструкция> дошлайфа този пост · /preview за визуален изглед · нов ред = нов пост"))

    def cmd_preview(self, _argument: str = "") -> None:
        """
        Генерира и отваря output/preview.html (текст + избрани снимки, самостоятелен файл) —
        виж src/pipeline/preview.py. Добавено по план 12 (research-plan-12-post-quality-and-ux.md,
        3.4): преди беше отделна CLI команда, лесна за забравяне; сега е една дума в REPL-а,
        предложена автоматично след всяко успешно пускане.
        """
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма генериран пост в тази сесия."))
            return
        from src.pipeline.preview import render_preview

        try:
            preview_path = render_preview(self.state.post_dir)
        except FileNotFoundError as exc:
            self.out(self.ink.err(f"  ✗ {exc}"))
            return
        _open_in_file_manager(preview_path)
        self.out(self.ink.dim(f"  Отворено: {preview_path}"))

    def _print_post(self, text: str) -> None:
        """
        Показва текста в рамка. Преди рамката съзнателно липсваше, за да се копира чист
        текст с маркиране от терминала — вече ненужно съображение, защото готовият пост
        се копира автоматично в клипборда (виж _generate), не чрез ръчно маркиране.
        """
        self.out("")
        self.out(self.ink.panel(text.strip(), title="Официално предложение", border_style="green3"))


def _open_in_file_manager(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # само на Windows
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def copy_to_clipboard(text: str) -> bool:
    """
    Копира текст в системния клипборд, без нова зависимост (pyperclip и др.) — всяка ОС
    вече носи вграден инструмент за това. Връща дали е успяло; никога не хвърля изключение,
    защото копирането е удобство, не част от резултата на конвейера (виж _generate).

    На Windows вика PowerShell Set-Clipboard през временен UTF-8 файл, не `clip.exe`
    директно — по-старите версии на `clip` презаписват кирилицата през грешен codepage.
    """
    try:
        if sys.platform.startswith("win"):
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
                handle.write(text)
                temp_path = handle.name
            try:
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 '{temp_path}')",
                    ],
                    check=True,
                    capture_output=True,
                )
            finally:
                os.unlink(temp_path)
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)
        return True
    except Exception:  # pragma: no cover — best-effort удобство, никога не бива да чупи сесията
        return False


def main() -> int:
    force_utf8_io()  # при `python -m src.repl`; през cli.py вече е извикано
    return Repl().run()


if __name__ == "__main__":
    sys.exit(main())
