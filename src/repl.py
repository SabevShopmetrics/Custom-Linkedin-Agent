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
from src.errors import AgentError, AuthenticationError
from src.humanize import humanize_notes, status_label
from src.llm.client import LLMClient
from src.logging_utils import register_persistent_handler, unregister_persistent_handler
from src.pipeline import modes, planner
from src.pipeline.consolidator import PipelineResult
from src.pipeline.preview import render_preview
from src.pipeline.runner import FinalCheckUpdate, refresh_photos, run_pipeline, run_revision
from src.pipeline.util import PHOTO_EXTENSIONS, file_uri, list_photos, read_text_or_none
from src.quality.metrics import edit_ratio
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

# research-plan-36, В5: кратката помощ съдържа само нещата, които маркетологът реално ползва;
# всичко останало е под „/help всички". Ревизия вече не изисква команда (виж
# looks_like_revision), затова /revise не е в кратката помощ.
HELP_TEXT = """\
Как се работи:
  1. Опиши с няколко изречения какво се е случило (можеш да поставиш цял имейл) и натисни Enter.
  2. Прочети предложението. За промяна просто напиши какво да се смени — напр. „по-кратко",
     „добави благодарност на ВСУ", „махни втория абзац".
  3. За нов пост напиши „нов: " и брифа (или само „нов").

Снимки: провлачи файла тук и Enter, или напиши „+" и ги избери с мишката.

  /чернова        показва първата чернова на последния пост
  /again          пише последния пост изцяло наново
  /publikuvano    запиши какво реално публикувахте (за да мерим качеството)
  /help всички    всички команди
  /exit           изход\
"""

HELP_TEXT_FULL = """\
Всички команди:
  нов: <бриф>       нов пост (само „нов" — пита за брифа и за снимки)
  /mode [режим]     дължина на следващия пост — Кадър/Фокус/Панорама (виж /mode без аргумент)
  +                 отваря диалог за избор на снимки с мишка (или /pick, /browse)
  /attach <път> …   прикачва снимка, папка със снимки или маска (*.jpg)
  /photos           показва прикаченото; /photos clear го изчиства
  /revise <текст>   променя последния пост (същото като просто да напишеш молбата)
  /again            пише последния пост изцяло наново (всички стъпки)
  /чернова          първата чернова на последния пост (/draft)
  /publikuvano      записва публикувания текст и показва колко е редактиран (/published)
  /preview          отваря визуален изглед (текст + избрани снимки)
  /last             показва последния пост още веднъж
  /подробно         показва/скрива техническите стъпки по време на писане (/verbose)
  /log [n]          последните n реда от run.log на последния пост (по подр. 30)
  /dir              папката на последния пост; /open я отваря
  /ключ             въвежда нов API ключ (/key)
  /check            проверява връзката със сървъра и всеки модел
  /knowledge        какво липсва в knowledge/
  /reload           препрочита knowledge/ и .env
  /clear            изчиства екрана
  /help             кратка помощ
  /exit             изход\
"""

# Ревизия без команда (research-plan-36, В3). Ред, който започва с повелителна форма за
# редакция или с „по-", след като вече има пост в сесията, се третира като промяна на него,
# а не като нов бриф. Нарочно тесен списък с ПОВЕЛИТЕЛНИ форми, следвани от интервал или край:
# „Добавихме нов член..." (минало време) не съвпада с „добави".
_REVISION_CUES = (
    "добави", "махни", "премахни", "смени", "промени", "съкрати", "скъси", "удължи", "направи",
    "пренапиши", "поправи", "оправи", "сложи", "замени", "преформулирай", "напиши", "без",
    "остави", "върни", "използвай", "ползвай", "спомени", "благодари", "започни", "завърши",
    "раздели", "обедини", "преведи", "изтрий", "махнете", "добавете", "сменете", "направете",
)
_REVISION_MAX_WORDS = 30


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


# Провлачен файл в PowerShell пристига като `& 'C:\път с интервали\a.png'` — операторът за
# извикване `&` и ЕДИНИЧНИ кавички; cmd слага двойни. Реален случай (2026-10-13): снимка,
# провлачена в PowerShell, не се разпозна и пътят ѝ замина към модела като текст на брифа.
_QUOTED_OR_BARE_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')
_POWERSHELL_CALL_OPERATOR = "&"


def split_path_args(text: str) -> list[str]:
    """
    Разбива една реплика на отделни пътища — поддържа "път с интервали" в двойни или
    единични кавички и пропуска `&`, който PowerShell слага пред провлачен файл.
    Споделено между /attach и /wizard (стъпката за снимки), за да приемат едно и също.
    """
    tokens = [double or single or bare for double, single, bare in _QUOTED_OR_BARE_TOKEN.findall(text)]
    return [token for token in tokens if token != _POWERSHELL_CALL_OPERATOR]


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


def bare_photo_paths(line: str) -> list[Path] | None:
    """
    Ако ЦЕЛИЯТ ред са само пътища до съществуващи снимки — най-естественото движение,
    провлачи файла(овете) направо на промпта и Enter, без @ отпред или /attach — разпознай
    ги автоматично, вместо да ги third-ираш буквално като текст на нов бриф.

    Връща None, ако дори ЕДИН токен на реда не се разрешава до снимка — тогава редът е
    обикновен текст (напр. "Ето my-photo.jpg от снощи" не бива тихо да изяде текста, само
    защото едно от думите му случайно съвпада с истински файл на диска).
    """
    tokens = split_path_args(line)
    if not tokens:
        return None
    found: list[Path] = []
    for token in tokens:
        matches = _expand_one(token)
        if not matches:
            return None
        found.extend(matches)
    return found or None


def unsupported_file_paths(line: str) -> list[Path] | None:
    """
    Ако ЦЕЛИЯТ ред са пътища до съществуващи файлове, но поне един не е поддържана снимка
    (.HEIC от iPhone, .gif, .pdf...) — връща тези файлове. Иначе None.

    research-plan-36, А9: досега такъв ред минаваше за бриф и пускаше цял конвейер върху
    текста „C:\\Users\\...\\IMG_0042.HEIC".
    """
    tokens = split_path_args(line)
    if not tokens:
        return None
    unsupported: list[Path] = []
    for token in tokens:
        candidate = Path(token.strip().strip('"').strip("'")).expanduser()
        if not candidate.is_file():
            return None
        if candidate.suffix.lower() not in PHOTO_EXTENSIONS:
            unsupported.append(candidate)
    return unsupported or None


def looks_like_revision(line: str) -> bool:
    """
    research-plan-36, В3: кратък ред, започващ с повелителна форма за редакция („по-кратко",
    „добави ...", „махни ...") е молба за промяна на последния пост, не нов бриф. Викащият
    проверява дали изобщо има пост в сесията.
    """
    words = line.strip().split()
    if not words or len(words) > _REVISION_MAX_WORDS:
        return False
    first = words[0].lower().strip(",.:;!—-")
    return first.startswith("по-") or first in _REVISION_CUES


def _stdin_has_pending_input() -> bool:
    """
    Има ли още символи в буфера на конзолата веднага след прочетен ред — тоест човекът е
    поставил текст на няколко реда наведнъж (research-plan-36, А6). Без това всеки ред от
    поставен имейл ставаше отделен пост — реален случай: три пускания от три абзаца.
    """
    time.sleep(0.03)  # поставеният текст пристига почти мигновено; ръчно писане — не
    try:
        if sys.platform.startswith("win"):
            import msvcrt

            return bool(msvcrt.kbhit())
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return bool(ready)
    except Exception:  # без конзола (пренасочен вход, IDE) — държим се като преди
        return False


def _default_file_picker() -> tuple[str, ...] | None:
    """
    Реалният системен диалог за избор на файлове (tkinter) — подразбиране за
    `Repl.file_picker_fn`, инжектирано в __init__ по същия модел като client_factory/
    pipeline_fn/clipboard_fn, за да могат тестовете да го подменят без реален GUI.

    Връща `None`, ако tkinter не е наличен в средата (напр. сървър без графичен
    интерфейс) — различно от празен тъпъл (`()`), който значи "потребителят не избра
    нищо" — cmd_pick_photos показва различно съобщение за двата случая.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    filetypes = [
        ("Снимки", " ".join(f"*{ext}" for ext in sorted(PHOTO_EXTENSIONS))),
        ("Всички файлове", "*.*"),
    ]
    try:
        return filedialog.askopenfilenames(title="Избери снимки за поста", filetypes=filetypes)
    finally:
        root.destroy()


# Провлачен файл вътре в брифа, без @: по желание `&` (PowerShell), после път в двойни,
# единични или никакви кавички.
_DRAGGED_PATH_TOKEN = re.compile(r'(?:&\s*)?(?:"([^"]+)"|\'([^\']+)\'|(\S+))')


def _dragged_photo(raw: str) -> Path | None:
    """
    Пълен път до съществуваща снимка — следа от провлачване, не от писане: никой не пише
    `C:\\Users\\...\\a.png` в бриф на ръка. Относително име („снимка.jpg") НЕ се приема —
    иначе дума от брифа, случайно съвпаднала с файл в текущата папка, би изчезнала тихо.
    """
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() and candidate.is_file() and candidate.suffix.lower() in PHOTO_EXTENSIONS:
        return candidate
    return None


def extract_inline_attachments(text: str) -> tuple[str, list[Path], list[str]]:
    """
    Изважда `@път` и провлачени пълни пътища до снимки от текста на брифа. @токен, който
    НЕ сочи към съществуваща снимка, остава в текста непокътнат — @споменаване в бриф е
    нормален български текст и не бива да се яде тихо.
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

    def replace_dragged(match: re.Match[str]) -> str:
        photo = _dragged_photo(match.group(1) or match.group(2) or match.group(3))
        if photo is None:
            return match.group(0)
        found.append(photo)
        return ""

    cleaned = _ATTACHMENT_TOKEN.sub(replace, text)
    cleaned = _DRAGGED_PATH_TOKEN.sub(replace_dragged, cleaned)
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

# research-plan-36, В1: какво вижда маркетологът вместо „[4] Критик върху Post I..." и
# „присъда: needs_revision". Технически изглед остава с /подробно. None → стъпката не се
# показва (фоновата проверка [7] съобщава сама, когато приключи).
_HUMAN_STAGES = {
    "[1]": "Чета брифа…",
    "[1.5]": "Чета брифа…",
    "[2]/[3]": "Пиша чернова…",
    "[4]": "Проверявам факти, тон и правила…",
    "[5]": "Подобрявам текста…",
    "[6]": "Подобрявам текста…",
    "[6б]": "Последни поправки…",
    "[Р]": "Редактирам поста…",
    "[7]": None,
}


class ProgressHandler(logging.Handler):
    """
    Показва стъпките на конвейера докато вървят, групирани по секции — минути мълчалив
    терминал изглеждат като забил процес, а плосък поток от редове без структура е също
    толкова безполезен: не личи кое принадлежи на коя стъпка. Всяка нова стъпка отваря
    удебелено заглавие; всичко, докладвано вътре в нея (план, находки, поправки, открити
    въпроси), виси отдолу с отстъп, докато не тръгне следваща стъпка.
    """

    def __init__(self, emit_line: Callable[[str], None], ink: Ink, *, verbose: bool = True) -> None:
        super().__init__(level=logging.INFO)
        self._emit_line = emit_line
        self._ink = ink
        # verbose=False (подразбиране в Repl от research-plan-36, В1): само кратки човешки етапи,
        # без детайли под тях. Класът сам по себе си остава с True — технически изглед.
        self.verbose = verbose
        self._current_tag: str | None = None
        self._last_stage: str | None = None
        # Етап, който REPL вече е показал сам (напр. „Чета брифа…" преди проверката за тънък
        # бриф) — следващото му появяване от лога се пропуска веднъж, за да не излиза два пъти.
        self.already_shown: str | None = None
        # Задава се отвън, само докато траят на екран прогрес-бар (виж _run_with_spinner) —
        # извиква се веднъж на всяка НОВА стъпка, с (таг, пълно съобщение). None извън
        # генериране — handler-ът си стои закачен цяла сесия, барът съществува само за
        # едно пускане.
        self.on_step: Callable[[str, str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("==="):
            # Ново пускане (или ревизия) — етапите се показват отначало, дори да съвпадат с
            # последните от предишното пускане.
            self._current_tag = None
            self._last_stage = None
        if message.startswith(_SUPPRESSED_LOG_PREFIXES):
            return

        match = _STEP_TAG_RE.match(message)
        tag = match.group(1) if match else None

        if not self.verbose:
            if not tag or tag == self._current_tag:
                return
            self._current_tag = tag
            stage = _HUMAN_STAGES.get(tag)
            if self.on_step:
                # Скрита стъпка ([7]) или непознат таг — лентата пази последния човешки етап,
                # вместо да покаже вътрешното име на стъпката.
                self.on_step(tag, stage or self._last_stage or "Работя…")
            if stage and stage == self.already_shown:
                self.already_shown = None
                self._last_stage = stage
                return
            if stage and stage != self._last_stage:
                self._last_stage = stage
                self._emit_line(self._ink.dim(f"  {stage}"))
            return

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
    mode: str = modes.DEFAULT_MODE
    verbose: bool = False  # research-plan-36, В1 — технически изглед само с /подробно


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
        file_picker_fn: Callable[[], tuple[str, ...] | None] = _default_file_picker,
        open_preview_fn: Callable[[Path], None] | None = None,
        env_path: Path | None = None,
        env_example_path: Path | None = None,
        today: date | None = None,
        revision_fn: Callable[..., PipelineResult] = run_revision,
        photo_refresh_fn: Callable[..., PipelineResult] = refresh_photos,
        pending_input_fn: Callable[[], bool] | None = None,
        secret_input_fn: Callable[[str], str] | None = None,
        verbose: bool | None = None,
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
        self.file_picker_fn = file_picker_fn
        self.open_preview_fn = open_preview_fn or _open_in_file_manager
        # Резолюция при създаване, не при дефиниция на функцията — тестовете подменят
        # модулните ENV_PATH/ENV_EXAMPLE_PATH, за да не пишат истински .env в репото.
        self.env_path = env_path or ENV_PATH
        self.env_example_path = env_example_path or ENV_EXAMPLE_PATH
        self.today = today
        self.revision_fn = revision_fn
        self.photo_refresh_fn = photo_refresh_fn
        # Откриване на поставен текст само при истинска конзола: тестовете (и всеки друг
        # инжектиран input_fn) получават „няма чакащ вход", тоест старото поведение ред по ред.
        if pending_input_fn is None:
            pending_input_fn = _stdin_has_pending_input if input_fn is input else (lambda: False)
        self.pending_input_fn = pending_input_fn
        if secret_input_fn is None:
            import getpass

            secret_input_fn = getpass.getpass if input_fn is input else input_fn
        self.secret_input_fn = secret_input_fn

        self.config: Config | None = None
        self.knowledge: BrandKnowledge | None = None
        self.state = SessionState()
        if verbose is None:
            verbose = os.environ.get("AGENT_VERBOSE", "").strip().lower() in {"1", "true", "да", "yes"}
        self.state.verbose = verbose
        self._cancel = threading.Event()
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
            # research-plan-36, А5: празен ключ в съществуващ .env беше задънена улица —
            # настройката не се предлагаше повече и единственият изход беше ръчна редакция.
            if "OPENWEBUI_API_KEY" in str(exc) and self.env_path.exists():
                self.out(self.ink.warn("Липсва API ключ."))
                if self._ask_for_api_key():
                    try:
                        self.config = self.config_loader()
                    except ConfigError as retry_exc:
                        self.out(self.ink.err(f"Грешка в настройките: {retry_exc}"))
                        return 1
                else:
                    return 1
            else:
                self.out(self.ink.err(f"Грешка в настройките: {exc}"))
                return 1

        try:
            self.knowledge = self.knowledge_loader()
        except Exception as exc:  # счупен YAML и т.н. — без traceback пред маркетолога
            self.out(
                self.ink.err(
                    f"Не успях да прочета знанията за клъстера (папка knowledge/): {exc}. "
                    f"Покажете това съобщение на колегата, който поддържа агента."
                )
            )
            return 1
        self._banner()

        # ProgressHandler препраща логовете на конвейера към конзолата в реално време,
        # докато е закачен — виж logging_utils.register_persistent_handler.
        handler = ProgressHandler(self.out, self.ink, verbose=self.state.verbose)
        self._progress_handler = handler
        register_persistent_handler(handler)
        try:
            while self._running:
                try:
                    line = self._read_input()
                except EOFError:
                    self.out("")
                    break
                except KeyboardInterrupt:
                    # research-plan-36, А7: много хора натискат Ctrl+C, за да копират — на празен
                    # промпт това вече не затваря цялата сесия.
                    self.out(self.ink.dim("\n  (За изход напишете /exit.)"))
                    continue
                if line is None or not line.strip():
                    continue
                try:
                    self.handle(line.strip())
                except KeyboardInterrupt:
                    # Ctrl+C извън генериране (напр. по време на въпрос) — само прекъсва текущото.
                    self.out(self.ink.warn("\n  ⚠ Прекъснато. Написаното дотук остава в папката на поста."))
                except Exception as exc:  # нарочно широко — един счупен пост не бива да
                    # сваля цялата сесия. AgentError вече е обработена по-надолу с човешко
                    # съобщение; тук остават само неочакваните (счупен път, права, бъг).
                    self.out(
                        self.ink.err(
                            "  ✗ Нещо се обърка неочаквано. Опитайте отново; ако се повтаря, покажете "
                            "това на колегата, който поддържа агента:"
                        )
                    )
                    self.out(self.ink.dim(f"    {type(exc).__name__}: {exc}"))
        finally:
            unregister_persistent_handler(handler)

        self.out(self.ink.dim("Дочуване."))
        return 0

    def _read_input(self) -> str | None:
        """
        Чете един логически ред; ред, завършващ с `\\`, продължава на следващия.

        research-plan-36, А6: поставен текст на няколко реда (имейл, покана) се събира в ЕДИН
        бриф — редовете, които вече чакат в буфера на конзолата веднага след първия, се
        прочитат заедно с него. Ръчно написан ред с Enter работи както преди.
        """
        line = self.input_fn(self.ink.accent(PROMPT))
        parts = [line]
        while parts[-1].rstrip().endswith("\\") or self.pending_input_fn():
            if parts[-1].rstrip().endswith("\\"):
                parts[-1] = parts[-1].rstrip()[:-1].rstrip()
                parts.append(self.input_fn(self.ink.dim(CONTINUATION_PROMPT)))
            else:
                parts.append(self.input_fn(""))
        return sanitize_input("\n".join(part for part in parts).strip())

    def _offer_env_setup(self) -> None:
        """
        Пита еднократно за OPENWEBUI_API_KEY и записва .env от .env.example — за да не се
        изисква от нетехнически човек да отваря и редактира файл, преди сесията да тръгне.
        Останалите настройки (модели, URL) остават каквито са в .env.example; който иска
        друг модел, си остава свободен да редактира .env ръчно после.
        """
        answer = self.input_fn(
            self.ink.accent("Първо стартиране — да настроя ли връзката сега? (Enter = да, 'не' за отказ) › ")
        )
        if answer.strip().lower() in {"не", "n", "no"}:
            self.out(self.ink.dim("  Пропуснато — създай .env ръчно от .env.example, преди да продължиш."))
            return
        self._ask_for_api_key()

    def _ask_for_api_key(self) -> bool:
        """
        Пита за API ключа (скрит при писане), записва го в .env и връща дали е записан.

        research-plan-36, А5: празен отговор НЕ записва файл — преди празният ключ се
        записваше, следващото стартиране виждаше, че .env съществува, и не питаше повече.
        Ключът не се проверява тук с мрежово извикване (бавно, при първо стартиране); грешен
        ключ вече спира веднага с ясно съобщение при първия пост (AuthenticationError).
        """
        self.out(
            self.ink.dim(
                "  API ключът е в Open WebUI (chat.velocity.online): иконката на профила → Settings → "
                "Account → API Keys. Ако нямате достъп, поискайте ключ от колегата, който поддържа агента."
            )
        )
        api_key = self.secret_input_fn(self.ink.accent("  Поставете ключа (не се вижда, докато пишете) › ")).strip()
        if not api_key:
            self.out(self.ink.warn("  Не е въведен ключ — нищо не е записано. Стартирайте пак, когато го имате."))
            return False

        if self.env_path.exists():
            template = self.env_path.read_text(encoding="utf-8")
        elif self.env_example_path.exists():
            template = self.env_example_path.read_text(encoding="utf-8")
        else:
            template = "OPENWEBUI_API_KEY=\n"
        if re.search(r"^OPENWEBUI_API_KEY=.*$", template, flags=re.MULTILINE):
            filled = re.sub(
                r"^OPENWEBUI_API_KEY=.*$", lambda _m: f"OPENWEBUI_API_KEY={api_key}", template, count=1, flags=re.MULTILINE
            )
        else:
            filled = template.rstrip("\n") + f"\nOPENWEBUI_API_KEY={api_key}\n"
        self.env_path.write_text(filled, encoding="utf-8")
        # load_dotenv(override=False) не презаписва вече заредена стойност — затова и тук.
        os.environ["OPENWEBUI_API_KEY"] = api_key
        self.out(self.ink.ok("  ✓ Ключът е записан."))
        return True

    def cmd_key(self, _argument: str = "") -> None:
        """/ключ — нов API ключ без ръчна редакция на .env (research-plan-36, А5)."""
        if self._ask_for_api_key():
            try:
                self.config = self.config_loader()
            except ConfigError as exc:
                self.out(self.ink.err(f"Грешка в настройките: {exc}"))

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

        # research-plan-36, В5: без ID на модела и без командите за напреднали — три неща,
        # които човек трябва да знае, за да започне.
        body = "\n".join(
            [
                f"Познавам {len(self.knowledge.members)} членове на клъстера и "
                f"{len(self.knowledge.good_examples)} ваши публикувани поста.",
                "",
                "✍  Опишете какво се е случило (може и поставен имейл) и натиснете Enter.",
                "📎 Снимки: провлачете файл тук или напишете „+“.",
                "🔁 След като видите поста, просто напишете какво да се промени.",
                "",
                "/help — помощ   ·   /exit — изход",
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

        # „нов: <бриф>" — изрично нов пост, дори текстът да прилича на молба за промяна. Само с
        # двоеточие: „Нов член се присъедини..." е истински бриф и думата „Нов" остава в него.
        keyword_match = re.match(r"^(нов|нов пост)\s*:\s*(.+)$", line.strip(), flags=re.IGNORECASE | re.DOTALL)
        if keyword_match:
            self._new_post(keyword_match.group(2).strip())
            return

        # "+" сам на реда отваря системния диалог за избор на файлове — аналог на "+"
        # бутона за прикачване в Claude Code, за човек, който предпочита мишка пред
        # ръчно писане/влачене на път. Виж cmd_pick_photos.
        if line.strip() == "+":
            self.cmd_pick_photos()
            return

        if not line.startswith("/"):
            # Целият ред е само път(и) до снимка(и), провлачени директно на промпта, без
            # @ отпред — най-интуитивното движение. Виж докстринга на bare_photo_paths.
            photos = bare_photo_paths(line)
            if photos is not None:
                self._photos_added(photos)
                return
            unsupported = unsupported_file_paths(line)
            if unsupported is not None:
                names = ", ".join(path.name for path in unsupported)
                self.out(
                    self.ink.err(
                        f"  ✗ {names} — не е снимка във формат, който агентът чете (JPG, PNG или WEBP). "
                        f"Снимка от iPhone (.HEIC): отворете я и я запазете/изпратете като JPG."
                    )
                )
                return
            # research-plan-36, В3: след готов пост кратка повелителна молба („по-кратко",
            # „добави ...") е ревизия — без да се помни командата /revise.
            if self._has_finished_post() and looks_like_revision(line):
                self.out(self.ink.dim("  (Приемам го като промяна на последния пост — за нов пост напишете „нов: …“.)"))
                self.cmd_revise(line)
                return
            self._new_post(line)
            return

        name, _, argument = line[1:].partition(" ")
        argument = argument.strip()
        commands: dict[str, Callable[[str], None]] = {
            "help": self.cmd_help,
            "?": self.cmd_help,
            "wizard": self.cmd_wizard,
            "w": self.cmd_wizard,
            "mode": self.cmd_mode,
            "attach": self.cmd_attach,
            "pick": self.cmd_pick_photos,
            "browse": self.cmd_pick_photos,
            "preview": self.cmd_preview,
            "photos": self.cmd_photos,
            "revise": self.cmd_revise,
            "again": self.cmd_again,
            "regen": self.cmd_again,
            "чернова": self.cmd_draft,
            "draft": self.cmd_draft,
            "publikuvano": self.cmd_published,
            "публикувано": self.cmd_published,
            "published": self.cmd_published,
            "подробно": self.cmd_verbose,
            "verbose": self.cmd_verbose,
            "ключ": self.cmd_key,
            "key": self.cmd_key,
            "помощ": self.cmd_help,
            "изход": self.cmd_exit,
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

    def cmd_help(self, argument: str = "") -> None:
        if argument.strip().lower() in {"всички", "all", "пълна"}:
            self.out(HELP_TEXT_FULL)
        else:
            self.out(HELP_TEXT)

    def cmd_verbose(self, _argument: str = "") -> None:
        """/подробно — превключва техническия изглед на стъпките (research-plan-36, В1)."""
        self.state.verbose = not self.state.verbose
        if self._progress_handler is not None:
            self._progress_handler.verbose = self.state.verbose
        self.out(
            self.ink.dim(
                "  Технически изглед: включен (стъпки, находки, модели)."
                if self.state.verbose
                else "  Технически изглед: изключен."
            )
        )

    def cmd_draft(self, _argument: str = "") -> None:
        """/чернова — първата чернова (post_i.md), напр. когато финалът е изгубил детайл (Г3)."""
        if self.state.post_dir is None:
            self.out(self.ink.dim("  Още няма пост в тази сесия."))
            return
        text = read_text_or_none(self.state.post_dir / "post_i.md")
        if not text:
            self.out(self.ink.dim("  Няма запазена чернова за последния пост."))
            return
        self.out("")
        self.out(self.ink.panel(text.strip(), title="Първа чернова (не е окончателният текст)", border_style="grey62"))

    def cmd_published(self, argument: str = "") -> None:
        """
        /publikuvano — Г6 (research-plan-36): човекът поставя текста, който реално е публикувал;
        записва се в output/published.md и се показва колко е редактиран. Това е единствената
        мярка, която отговаря директно на въпроса „публикуем ли е текстът без поправки" —
        bench я чете от published.md.
        """
        if self.state.post_dir is None or not self._has_finished_post():
            self.out(self.ink.dim("  Още няма готов пост в тази сесия."))
            return
        text = argument.strip()
        if not text:
            text = self.input_fn(
                self.ink.accent("  Поставете публикувания текст (Enter = същият като предложението) › ")
            ).strip()
            while self.pending_input_fn():
                text += "\n" + self.input_fn("")
        proposed = read_text_or_none(self.state.post_dir / "output" / "post.md") or ""
        published = text.strip() or proposed.strip()
        (self.state.post_dir / "output" / "published.md").write_text(published + "\n", encoding="utf-8")
        ratio = edit_ratio(proposed, published)
        if ratio == 0:
            self.out(self.ink.ok("  ✓ Записано: публикуван без промени."))
        else:
            self.out(self.ink.ok(f"  ✓ Записано: около {round(ratio * 100)}% от текста е променен преди публикуване."))

    def _has_finished_post(self) -> bool:
        return self.state.post_dir is not None and (self.state.post_dir / "output" / "post.md").exists()

    def _photos_added(self, photos: list[Path]) -> None:
        """
        Общ край за провлачени / избрани с мишка / прикачени снимки. research-plan-36, В4: ако
        вече има готов пост, пита дали снимките са за него — досега те тихо чакаха следващия пост.
        """
        if not photos:
            return
        if self._has_finished_post() and self.config is not None:
            answer = self.input_fn(
                self.ink.accent(
                    f"  {len(photos)} снимка(и). Да ги добавя ли към последния пост? "
                    f"(Enter = да, 'не' = пазя ги за следващия пост) › "
                )
            ).strip().lower()
            if answer not in {"не", "n", "no"}:
                self._add_photos_to_last_post(photos)
                return
        self.state.attachments.extend(photos)
        self.out(self.ink.ok(f"  + {len(photos)} снимка(и) за следващия пост; общо: {len(self.state.attachments)}"))

    def _add_photos_to_last_post(self, photos: list[Path]) -> None:
        assert self.state.post_dir is not None and self.config is not None
        copied = copy_attachments(photos, self.state.post_dir / "photos")
        self.out(self.ink.dim(f"  Добавени {len(copied)} снимка(и). Избирам снимките наново…"))
        client = self.client_factory(self.config)
        try:
            result = self.photo_refresh_fn(self.state.post_dir, client, knowledge=self.knowledge)
        except AgentError as exc:
            self.out(self.ink.err(f"  ✗ {exc.human_message}"))
            return
        selected = sorted((self.state.post_dir / "output" / "selected").glob("*"))
        names = ", ".join(path.name for path in selected) or "нито една"
        self.out(self.ink.ok(f"  ✓ Избрани снимки: {names}"))
        for note in humanize_notes(result.warnings + [n for n in result.open_notes if "Media Picker" in n]):
            self.out(self.ink.warn(f"  ⚠ {note}"))
        self._refresh_preview(self.state.post_dir, announce=True)

    def cmd_exit(self, _argument: str = "") -> None:
        self._running = False

    def cmd_clear(self, _argument: str = "") -> None:
        self.out("\033[2J\033[H" if self.ink.enabled else "")

    def cmd_mode(self, argument: str) -> None:
        """
        Дължина на СЛЕДВАЩИЯ генериран пост (виж pipeline/modes.py) — Кадър/Фокус/Панорама.
        Без аргумент показва текущия избор и опциите; с аргумент го сменя. Важи за цялата
        сесия (включително /again, /revise), докато не се смени пак — не се пита при всеки
        пост, същия принцип като снимките в /wizard.
        """
        if not argument:
            current = modes.get_mode(self.state.mode)
            self.out(f"  Текущ режим: {self.ink.accent(current.label)}")
            self.out(self.ink.dim(f"  Налични: {modes.mode_list_text()}"))
            self.out(self.ink.dim("  Смяна: /mode кадър · /mode фокус · /mode панорама"))
            return
        key = modes.resolve_mode(argument)
        if key is None:
            self.out(self.ink.err(f"Непознат режим '{argument}'. Налични: {modes.mode_list_text()}"))
            return
        self.state.mode = key
        chosen = modes.get_mode(key)
        self.out(self.ink.ok(f"  ✓ Режим: {chosen.label} — важи от следващия пост нататък."))

    def cmd_attach(self, argument: str) -> None:
        if not argument:
            self.out(self.ink.err("Употреба: /attach <път до снимка, папка или маска>"))
            return
        found, unresolved = resolve_attachments(split_path_args(argument))
        self._photos_added(found)
        for item in unresolved:
            self.out(self.ink.err(f"  ✗ не намерена снимка: {item}"))

    def cmd_pick_photos(self, _argument: str = "") -> None:
        """
        Отваря системния диалог за избор на файлове (мишка, без ръчно писане/влачене на
        път) — аналог на "+" бутона за прикачване в Claude Code. Достъпен и с бърза дума
        "+" сама на реда, и с /pick или /browse. Резултатът е идентичен на /attach — просто
        друг начин да се стигне дотам, за човек, който предпочита да разгледа папките си
        визуално.

        Самият диалог (`self.file_picker_fn`) е инжектиран в __init__ по подразбиране към
        `_default_file_picker` (tkinter) — същия модел като client_factory/pipeline_fn/
        clipboard_fn — за да могат тестовете да го подменят без реален графичен интерфейс.
        """
        paths = self.file_picker_fn()
        if paths is None:
            self.out(self.ink.err("  Диалог за избор на файлове не е наличен в тази среда — ползвай /attach <път>."))
            return
        if not paths:
            self.out(self.ink.dim("  Нищо не е избрано."))
            return

        found, unresolved = resolve_attachments(paths)
        self._photos_added(found)
        for item in unresolved:
            self.out(self.ink.err(f"  ✗ не разпознат файл: {item}"))

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

        Инструкцията на /revise печели над sticky режима на сесията, когато пряко му
        противоречи (напр. "по-кратко" докато "Панорама" е активна) — потребителска
        инструкция 2026-08-26, след живо тестване хвана точно обратния резултат: режимът
        налагаше долна граница от думи, Критикът маркираше по-краткия опит hard_reject, и
        Post II го НАДУВАШЕ обратно, противоположно на молбата. Виж
        modes.resolve_mode_for_revision — само ЗА ТАЗИ ревизия; self.state.mode (sticky,
        важи за следващия НОВ пост) не се пипа тук.
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

        revision_mode = modes.resolve_mode_for_revision(self.state.mode, argument)
        if revision_mode != self.state.mode:
            label = modes.get_mode(revision_mode).label
            self.out(self.ink.dim(f"  ({label} само за тази промяна)"))

        # research-plan-36, Б2: лека ревизия — Писателят редактира готовия пост с едно
        # извикване (вместо пълното пускане отначало). Молбата остава записана в briefing.md
        # по-горе, така че /again по-късно я взима предвид. runner.run_revision сам пада към
        # пълното пускане, когато лекият път няма смисъл (няма готов пост, недостатъчен бриф).
        self._generate(
            self.state.post_dir,
            force=True,
            mode_override=revision_mode,
            revision_instruction=argument.strip(),
        )

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

    def _looks_like_echoed_output(self, brief: str) -> bool:
        """
        Пази срещу честа грешка при поставяне: инструментът копира готовия текст на всеки
        пост в клипборда автоматично ("📋 копирано в клипборда"), и ако следващото поставяне
        влезе в тази реплика вместо нов бриф, "нов ред = нов пост" разцепва готовия текст на
        отделни "брифове" — реален случай (живо тестване, 2026-08-28): три отделни пускания
        се получиха от трите абзаца на току-що генерирания пост, всяко похарчило поне едно
        извикване към Планера, преди човекът да усети и да прекъсне с Ctrl+C.

        Проверката е евтина и локална: сравнява само срещу последния генериран пост В ТАЗИ
        сесия (self.state.post_dir), не срещу целия архив в posts/ — не е антиплагиатски
        филтър, а бекъп само за точно този пропуск.
        """
        if self.state.post_dir is None:
            return False
        last_text = read_text_or_none(self.state.post_dir / "output" / "post.md")
        if not last_text:
            return False
        candidate = brief.strip()
        # Прагът от 40 знака пази от лъжливи положителни резултати при кратки, общи изречения
        # (напр. "Благодарим на всички!"), които реално биха могли да съвпаднат случайно.
        return len(candidate) >= 40 and candidate in last_text

    def _start_post(self, brief: str, attachments: list[Path]) -> None:
        """Общ край за нов пост — от свободен ред или от /wizard: папка, briefing.md, снимки, генериране."""
        if self._looks_like_echoed_output(brief):
            answer = self.input_fn(
                self.ink.warn(
                    "  ⚠ Това прилича на текст от последния генериран пост, не на нов бриф — "
                    "случайно поставяне от клипборда? (Enter = продължи все пак, 'не' за отказ) › "
                )
            )
            if answer.strip().lower() in {"не", "n", "no"}:
                self.out(self.ink.dim("  Пропуснато."))
                return

        post_dir = unique_post_dir(self.posts_dir, slugify(brief), today=self.today)
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")

        copied = copy_attachments(attachments, post_dir / "photos")
        self.state.post_dir = post_dir
        self.state.brief = brief
        self.state.revisions = 0

        self.out(self.ink.dim("  " + (f"{len(copied)} снимка(и) · " if copied else "без снимки · ") + "Чета брифа…"))
        if self._progress_handler is not None:
            self._progress_handler.already_shown = "Чета брифа…"

        brief, enriched = self._thin_brief_precheck(post_dir, brief)
        if enriched:
            self.state.brief = brief
        self._generate(post_dir, force=enriched)

    def _thin_brief_precheck(self, post_dir: Path, brief: str) -> tuple[str, bool]:
        """
        Вариант 2 от research-plan-24-institution-gdpr-false-positive-and-word-floor.md: ако
        Планерът класифицира брифа като "тънък", пита ЕДНО насочено уточняващо въпросче ПРЕДИ
        да тръгне скъпият Писател/Критик/Консолидатор цикъл — вместо да остави Писателя да се
        бори между измисляне на факти и честна краткост чак след похабени извиквания (виж
        knowledge/voice/tone.md, правилото "спри честно, не пълни").

        Вика Планера директно тук (не през run_pipeline) — резултатът се кешира в plan.json
        точно както при нормално пускане (виж planner.py::should_skip), затова ако човекът
        не добави нищо, следващото run_pipeline() НЕ прави повторно извикване, само чете кеша.

        Ако брифът се обогати, старият plan.json вече не отговаря на новия текст и се трие,
        за да не поднесе Писателят план, изчислен върху по-тънката версия — force=True кара
        run_pipeline да преизчисли всичко наново.

        Връща (евентуално обогатен бриф, дали брифът е променен).
        """
        assert self.config is not None and self.knowledge is not None
        client = self.client_factory(self.config)
        try:
            plan = planner.run(post_dir, client, self.knowledge.stable_context, brief, force=False)
        except AgentError:
            return brief, False  # инфраструктурна грешка — нека реалното пускане отдолу я покаже

        if not planner.notes_flag_thin_brief(plan.notes):
            # planner.notes_flag_thin_brief разграничава истински оскъден бриф от "тънък, но
            # достатъчен/съдържателен" — установената идиома на самия Планер за "кратко, но
            # има достатъчно за пост". Реален случай от живо тестване (2026-08-31, виж
            # research-plan-25): бриф от 200+ думи с конкретни числа (€200 000 фонд, 15-членни
            # делегации) пак получи думата "тънък" в notes, защото Планерът винаги я ползва за
            # тази категория. Споделена с runner.py (Б-3 находка за дължина) — виж
            # research-plan-26, констатацията защо двете места трябва да ползват една проверка.
            return brief, False

        answer = self.input_fn(
            self.ink.dim(
                "  Брифът е доста кратък — има ли още нещо конкретно за добавяне "
                "(число, име, резултат)? (Enter = продължи както е) › "
            )
        ).strip()
        if not answer:
            return brief, False

        enriched_brief = f"{brief}\n\n{answer}"
        (post_dir / "briefing.md").write_text(enriched_brief + "\n", encoding="utf-8")
        (post_dir / "plan.json").unlink(missing_ok=True)
        return enriched_brief, True

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

        Ctrl+C (research-plan-36, А7): първото натискане вдига `self._cancel` — конвейерът спира
        на следващата граница между стъпки (runner._check_cancel) и тук се изчаква това да
        стане, за да не продължи фоновата нишка да вика модели и да печата върху следващия
        промпт. Второ натискане спира чакането веднага.
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
            TimeElapsedColumn(),
            console=_panel_console(),
            transient=True,  # барът изчезва при край — редовете, вече отпечатани, остават
        )
        task_id = progress.add_task("Започвам…", total=len(_STEP_ORDER))

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
                    try:
                        thread.join(timeout=0.2)
                    except KeyboardInterrupt:
                        if self._cancel.is_set():
                            raise  # второ Ctrl+C — не чакаме повече
                        self._cancel.set()
                        progress.update(task_id, description="Спирам след текущата стъпка… (Ctrl+C пак = веднага)")
            finally:
                self.out = original_out
                if handler:
                    handler.on_step = None

        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["result"]  # type: ignore[return-value]

    def _show_draft(self, text: str) -> None:
        """
        Б1 (research-plan-36): първата чернова на екрана веднага щом е написана — човекът чете,
        докато проверката и подобряването текат, вместо да гледа празен екран 3–7 минути.
        Вика се от нишката на конвейера; `self.out` по това време сочи към конзолата на
        прогрес-бара (виж _run_with_spinner), която rich синхронизира сама.
        """
        self.out("")
        self.out(
            self.ink.panel(
                text,
                title="Чернова — още я проверявам и подобрявам",
                border_style="grey62",
            )
        )

    def _generate(
        self,
        post_dir: Path,
        *,
        force: bool,
        mode_override: str | None = None,
        revision_instruction: str | None = None,
    ) -> None:
        """
        Пуска конвейера върху `post_dir` и отпечатва резултата/грешката. Общ път за нов пост,
        /again, /revise. `mode_override` — само от /revise, когато инструкцията пряко
        противоречи на sticky режима (виж cmd_revise); None означава "ползвай sticky режима
        на сесията", както преди. `revision_instruction` — лека ревизия (runner.run_revision)
        вместо пълното пускане (research-plan-36, Б2).
        """
        assert self.config is not None
        effective_mode = mode_override or self.state.mode
        # Нов клиент на всяко пускане — виж докстринга на модула защо (call_log в meta.json).
        client = self.client_factory(self.config)
        started = time.monotonic()
        self._cancel.clear()

        if revision_instruction is not None:
            call = lambda: self.revision_fn(  # noqa: E731
                post_dir,
                client,
                revision_instruction,
                knowledge=self.knowledge,
                mode=effective_mode,
                today=self.today,
                on_final_check=self._show_final_check_update,
                should_cancel=self._cancel.is_set,
            )
        else:
            call = lambda: self.pipeline_fn(  # noqa: E731
                post_dir,
                client,
                force=force,
                knowledge=self.knowledge,
                mode=effective_mode,
                today=self.today,
                on_final_check=self._show_final_check_update,
                on_draft=self._show_draft,
                should_cancel=self._cancel.is_set,
            )

        try:
            result = self._run_with_spinner(call)
        except AgentError as exc:
            # Критикът вече не спира конвейера (виж runner.py), така че тук идват само
            # входни и инфраструктурни грешки. Ако въпреки това на диска има текст от
            # прекъснато пускане, показваме го — по-полезно е от чисто съобщение за грешка.
            partial = read_text_or_none(post_dir / "output" / "post.md")
            if partial:
                self._print_post(partial)
                self.out(self.ink.warn("  ⚠ Това е текст от предишно или прекъснато пускане, не новото предложение."))
            self.out(self.ink.err(f"  ✗ {exc.human_message}"))
            if isinstance(exc, AuthenticationError):
                self.out(self.ink.dim("  Напишете /ключ, за да въведете нов ключ."))
            # research-plan-36, В7: суровата причина (HTTP код, модел) е само в run.log —
            # показва се на екрана единствено в технически изглед.
            if exc.detail and self.state.verbose:
                self.out(self.ink.dim(f"  Причина: {exc.detail}"))
            return

        # Успешен път: конвейерът винаги стига дотук с готово предложение (виж runner.py).
        elapsed = time.monotonic() - started
        text = (read_text_or_none(result.post_path) or "").strip()
        self._print_post(text)

        if self.state.verbose:
            if result.resolution_summary:
                self.out(self.ink.dim(f"  {result.resolution_summary}"))
            for fix in result.applied_fixes:
                self.out(self.ink.ok(f"  ✎ {fix}"))
        raw_notes = result.open_notes + result.warnings
        # research-plan-36, В2: бележките на човешки език, с действие — без members.yaml,
        # mention_allowed, Post II и т.н. Суровите остават в meta.json и run.log.
        notes = raw_notes if self.state.verbose else humanize_notes(raw_notes)
        if notes:
            self.out("")
            self.out(self.ink.warn("  Проверете преди публикуване:"))
            for note in notes:
                self.out(self.ink.warn(f"  ⚠ {note}"))

        # Статус, копиране и линк за браузъра НАРОЧНО последни — след всички бележки, не преди
        # тях (потребителска обратна връзка 2026-08-25: "отдолу да дава линк за в браузъра" —
        # с дълъг списък бележки линкът по-рано лесно се скриваше нагоре в скрола на терминала).
        words = len(text.split())
        status_text = status_label(result.status)
        status = self.ink.ok(f"✓ {status_text}") if result.status == "ready" else self.ink.warn(f"⚠ {status_text}")
        details = f"· {words} думи · {elapsed:.0f} сек."
        if self.state.verbose:
            merged = " + ".join(result.merged_from) or "?"
            details += f" · от {merged} · {len(client.call_log)} извиквания · {modes.get_mode(effective_mode).label}"
        self.out(f"  {status} {self.ink.dim(details)}")

        # Копиране в клипборда вместо ръчно маркиране от терминала — best-effort, тих провал.
        if text and self.clipboard_fn(text):
            self.out(self.ink.dim("  📋 Текстът е копиран — поставете го направо в LinkedIn (Ctrl+V)."))

        selected_dir = post_dir / "output" / "selected"
        selected = sorted(selected_dir.glob("*")) if selected_dir.is_dir() else []
        if selected:
            self.out(self.ink.dim(f"  🖼  Избрани снимки ({len(selected)}): {selected_dir}"))

        self._refresh_preview(post_dir, announce=True, open_it=True)

        self.out(
            self.ink.dim(
                "  Какво следва: напишете какво да се промени (напр. „по-кратко“) · „нов: …“ за нов пост · /help"
            )
        )
        if not self.state.verbose and not result.insufficient_brief:
            self.out(self.ink.dim("  Проверявам окончателния текст още веднъж във фонов режим — ще ви кажа, ако намеря нещо."))

        # B4 (виж research-plan-18-critique-of-plan-17.md, А2): Планерът вече прецени, че
        # брифът не носи никакво реално съдържание — постът по-горе е буквалната чернова на
        # Писателя, не преминала през Критик/Post II. Пита директно, вместо тихо да остави
        # изцяло общ текст да изглежда като напълно готово предложение.
        if result.insufficient_brief:
            answer = self.input_fn(
                self.ink.accent(
                    "  Брифът не стига за истински пост (или темата е от забранените). Напишете още "
                    "подробности сега, за да опитам отново — или Enter, за да оставите текста така › "
                )
            ).strip()
            if answer:
                briefing_path = post_dir / "briefing.md"
                existing = (read_text_or_none(briefing_path) or "").rstrip("\n")
                briefing_path.write_text(f"{existing}\n{answer}\n", encoding="utf-8")
                self._generate(post_dir, force=True, mode_override=mode_override)

    def _refresh_preview(self, post_dir: Path, *, announce: bool = False, open_it: bool = False) -> None:
        """
        Пише output/preview.html наново (best-effort). `open_it` — отваря го автоматично
        (потребителска заявка 2026-09-03). Вика се и след фоновата проверка [7], за да влязат
        закъснелите бележки в прегледа (research-plan-36, В6) — тогава без повторно отваряне.
        """
        try:
            preview_path = render_preview(post_dir)
        except (FileNotFoundError, OSError) as exc:
            if announce:
                self.out(self.ink.dim(f"  ⚠ Прегледът не се генерира: {exc}"))
            return
        if announce:
            self.out(self.ink.accent(f"  🔗 Как ще изглежда в LinkedIn: {file_uri(preview_path)}"))
        if open_it:
            try:
                self.open_preview_fn(preview_path)
            except Exception:
                pass

    def _show_final_check_update(self, update: FinalCheckUpdate) -> None:
        """
        Извикано от daemon thread-а на стъпка [7] (виж runner.py::_start_async_final_check),
        често секунди до минути СЛЕД като `_generate` вече е върнала контрол на потребителя —
        възможно е той вече да седи на подканата за следваща команда или дори да е започнал
        нов пост. Печата директно (само best-effort подредба с водещ нов ред), вместо да чака
        следваща команда — известното ограничение (TUI/REPL не се обновяват визуално при
        закъсняла находка от стъпка 7) вече има видим изход тук, не само запис в meta.json.

        research-plan-36, В2/В6: бележките са на човешки език и прегледът (preview.html) се
        обновява, за да ги съдържа — досега закъснелите бележки никога не стигаха до него.
        """
        is_current = self.state.post_dir == update.post_dir
        label = "" if is_current else f" (пост „{update.post_dir.name}“)"
        self._refresh_preview(update.post_dir)
        if update.status == "failed":
            if self.state.verbose:
                self.out(self.ink.warn(f"\n  ⚠ Окончателната проверка{label} не успя: {update.error}"))
            else:
                self.out(self.ink.dim(f"\n  (Окончателната проверка{label} не успя — прочетете поста внимателно.)"))
            return
        notes = update.notes if self.state.verbose else humanize_notes(update.notes)
        if not notes:
            self.out(self.ink.ok(f"\n  ✓ Окончателната проверка{label} завърши — няма нови бележки."))
            return
        self.out(self.ink.warn(f"\n  ⚠ Окончателната проверка{label} намери още за преглед:"))
        for note in notes:
            self.out(self.ink.warn(f"    · {note}"))

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
        try:
            preview_path = render_preview(self.state.post_dir)
        except FileNotFoundError as exc:
            self.out(self.ink.err(f"  ✗ {exc}"))
            return
        self.open_preview_fn(preview_path)
        self.out(self.ink.dim(f"  Отворено: {preview_path}"))

    def _print_post(self, text: str) -> None:
        """
        Показва текста в рамка. Преди рамката съзнателно липсваше, за да се копира чист
        текст с маркиране от терминала — вече ненужно съображение, защото готовият пост
        се копира автоматично в клипборда (виж _generate), не чрез ръчно маркиране.
        """
        self.out("")
        self.out(self.ink.panel(text.strip(), title="Предложение за публикуване", border_style="green3"))


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
