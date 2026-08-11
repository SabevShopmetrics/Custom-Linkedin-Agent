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
"""

from __future__ import annotations

import glob as globlib
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

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
PROMPT = "› "
CONTINUATION_PROMPT = "  "

HELP_TEXT = """\
Пиши брифа на един ред и натисни Enter — агентът генерира поста.
Ред, завършващ с \\, продължава на следващия (за по-дълъг бриф).
Снимка се прикачва с @път вътре в брифа или с /attach.

  /attach <път> …   прикачва снимка, папка със снимки или маска (*.jpg)
  /photos           показва прикаченото; /photos clear го изчиства
  /revise <текст>   пренаписва последния пост с допълнителна инструкция
  /again            пуска последния бриф наново (всички стъпки от нула)
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


@dataclass(frozen=True)
class Ink:
    """Минимален ANSI слой. При `enabled=False` всеки метод връща текста непокътнат."""

    enabled: bool = True

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def ok(self, text: str) -> str:
        return self._wrap("32", text)

    def warn(self, text: str) -> str:
        return self._wrap("33", text)

    def err(self, text: str) -> str:
        return self._wrap("31", text)

    def accent(self, text: str) -> str:
        return self._wrap("36", text)


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
    latin = "".join(_TRANSLIT.get(char, char) for char in text.lower())
    words = re.findall(r"[a-z0-9]+", latin)
    slug = "-".join(words[:max_words])[:max_length].strip("-")
    return slug or "post"


def unique_post_dir(posts_dir: Path, slug: str, *, today: date | None = None) -> Path:
    """posts/<дата>-<slug>, с наставка -2, -3 … ако вече е зает."""
    stamp = (today or date.today()).isoformat()
    base = f"{stamp}-{slug}"
    candidate = posts_dir / base
    counter = 2
    while candidate.exists():
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
    "GDPR предупреждение:",  # runner.py логва находките и втори път, накуп
    "Спряно:",  # следва го human_message на AgentError, дословно същото
    "Финален статус:",  # резюмето след поста го казва по-кратко
    "Открит въпрос:",  # излиза накуп след поста, където му е мястото
    "Папката 'photos/'",  # media_picker логва празната папка, runner я логва пак
)


class ProgressHandler(logging.Handler):
    """Показва стъпките на конвейера докато вървят — минути мълчалив терминал изглеждат
    като забил процес."""

    def __init__(self, emit_line: Callable[[str], None], ink: Ink) -> None:
        super().__init__(level=logging.INFO)
        self._emit_line = emit_line
        self._ink = ink

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(_SUPPRESSED_LOG_PREFIXES):
            return
        if record.levelno >= logging.WARNING:
            self._emit_line(self._ink.warn(f"  ⚠ {message}"))
        else:
            self._emit_line(self._ink.dim(f"  · {message}"))


# --------------------------------------------------------------------------- състояние


@dataclass
class SessionState:
    attachments: list[Path] = field(default_factory=list)
    post_dir: Path | None = None
    brief: str = ""
    revisions: int = 0


# --------------------------------------------------------------------------- REPL


class Repl:
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
        self.today = today

        self.config: Config | None = None
        self.knowledge: BrandKnowledge | None = None
        self.state = SessionState()
        self._running = True

    # ------------------------------------------------------------------ цикъл

    def run(self) -> int:
        try:
            self.config = self.config_loader()
        except ConfigError as exc:
            self.out(self.ink.err(f"Грешка в конфигурацията: {exc}"))
            return 1

        self.knowledge = self.knowledge_loader()
        self._banner()

        handler = ProgressHandler(self.out, self.ink)
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

    def _banner(self) -> None:
        assert self.config is not None
        self.out(self.ink.bold("LinkedIn агент — ИКТ Клъстер Варна"))
        self.out(self.ink.dim(f"{self.config.openwebui_base_url} · писател: {self.config.model_writer}"))
        self.out(self.ink.dim("/help за командите. Иначе просто напиши за какво е постът."))
        self.out("")

    # ------------------------------------------------------------------ команди

    def handle(self, line: str) -> None:
        if not line.startswith("/"):
            self._new_post(line)
            return

        name, _, argument = line[1:].partition(" ")
        argument = argument.strip()
        commands: dict[str, Callable[[str], None]] = {
            "help": self.cmd_help,
            "?": self.cmd_help,
            "attach": self.cmd_attach,
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
        raw_args = [quoted or bare for quoted, bare in re.findall(r'"([^"]+)"|(\S+)', argument)]
        found, unresolved = resolve_attachments(raw_args)
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
        brief, inline_photos, unresolved = extract_inline_attachments(line)
        for item in unresolved:
            self.out(self.ink.dim(f"  (@{item} не е съществуваща снимка — остава като текст в брифа)"))
        if not brief:
            self.out(self.ink.err("Брифът е празен — опиши повода за поста."))
            return

        attachments = self.state.attachments + inline_photos
        post_dir = unique_post_dir(self.posts_dir, slugify(brief), today=self.today)
        post_dir.mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief + "\n", encoding="utf-8")

        copied = copy_attachments(attachments, post_dir / "photos")
        self.state.attachments.clear()
        self.state.post_dir = post_dir
        self.state.brief = brief
        self.state.revisions = 0

        self.out(self.ink.dim(f"  {post_dir}" + (f" · {len(copied)} снимка(и)" if copied else " · без снимки")))
        self._generate(post_dir, force=False)

    def _generate(self, post_dir: Path, *, force: bool) -> None:
        assert self.config is not None
        client = self.client_factory(self.config)
        started = time.monotonic()

        try:
            result = self.pipeline_fn(post_dir, client, force=force, knowledge=self.knowledge)
        except AgentError as exc:
            # Критикът вече не спира конвейера (виж runner.py), така че тук идват само
            # входни и инфраструктурни грешки. Ако въпреки това на диска има текст от
            # прекъснато пускане, показваме го — по-полезно е от чисто съобщение за грешка.
            partial = read_text_or_none(post_dir / "output" / "post.md")
            if partial:
                self._print_post(partial)
                self.out(self.ink.warn("  ⚠ Това е текст от прекъснато пускане, не финалното предложение."))
            self.out(self.ink.err(f"  ✗ Спряно: {exc.human_message}"))
            self.out(self.ink.dim(f"  Пълен лог: {post_dir / 'run.log'} · /log го показва тук"))
            return

        elapsed = time.monotonic() - started
        text = read_text_or_none(result.post_path) or ""
        self._print_post(text)

        merged = " + ".join(result.merged_from) or "?"
        status = self.ink.ok("✓ предложение") if result.status == "ready" else self.ink.warn("⚠ предложение с бележки")
        calls = len(client.call_log)
        self.out(f"  {status} {self.ink.dim(f'· от {merged} · {calls} извиквания · {elapsed:.0f}s')}")
        self.out(self.ink.dim(f"  {result.post_path}"))

        for fix in result.applied_fixes:
            self.out(self.ink.ok(f"  ✎ {fix}"))
        for note in result.open_notes:
            self.out(self.ink.warn(f"  ⚠ {note}"))
        for warning in result.warnings:
            self.out(self.ink.warn(f"  ⚠ {warning}"))
        if result.open_notes or result.warnings:
            self.out(self.ink.dim("  Бележките не спират поста — прегледай ги преди публикуване."))
        self.out(self.ink.dim("  /revise <инструкция> дошлайфа този пост · нов ред = нов пост"))

    def _print_post(self, text: str) -> None:
        """Текстът се печата без рамка — така се копира чист от терминала."""
        rule = self.ink.dim("─" * 60)
        self.out("")
        self.out(rule)
        self.out(text.strip())
        self.out(rule)


def _open_in_file_manager(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # само на Windows
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main() -> int:
    force_utf8_io()  # при `python -m src.repl`; през cli.py вече е извикано
    return Repl().run()


if __name__ == "__main__":
    sys.exit(main())
