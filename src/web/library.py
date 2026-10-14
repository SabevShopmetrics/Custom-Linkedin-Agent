"""
Папката на компютъра, която агентът чете сам (docs/technical/web-ui-3.md).

web-ui-2 приемаше, че сървърът е „в мрежата" и няма достъп до диска на човека, затова
браузърът качваше графика и снимките. Но start_web.bat пуска сървъра на СЪЩИЯ компютър
(127.0.0.1) — той може да чете диска директно. Човекът посочва една папка веднъж; оттам
нататък агентът сам вижда:

  албуми   — подпапки със снимки („2026-10-20 среща", „Стажанти октомври"); датата се
             познава от началото на името, ако я има
  графици  — .xlsx/.csv/.txt/.json файлове, които приличат на график

Нищо не се качва и нищо в папката не се променя — само се чете. Снимките се копират в
папката на поста едва когато човекът (или одобрен план) ги свърже с пост.

Сигурност: папката се избира само от този компютър (заявка от 127.0.0.1). Колега от
мрежата вижда само вече избраната папка, никога останалия диск (виж server._local_only).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.pipeline.util import read_json, write_json
from src.web import schedule

SETTINGS_FILE = "_settings.json"  # в posts/ — „_" го крие от историята, както _batches
THUMBS_DIR = "_thumbs"  # кеш с миниатюри на снимките от папката (може да се изтрие)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".jfif"}
TEXT_SUFFIXES = {".txt", ".md"}
MAX_ALBUM_TEXT = 4000
_TEXT_HINT = re.compile(r"бриф|текст|пост|описание|инфо|brief|text|post|caption|info", re.IGNORECASE)
MAX_DEPTH = 3
MAX_ENTRIES = 5000
MAX_ALBUM_PHOTOS = 24
_SCHEDULE_HINT = re.compile(r"граф|grafik|schedule|план|plan|пост|post|календар|calendar", re.IGNORECASE)
_LEADING_DATE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[._]\d{1,2}(?:[._]\d{2,4})?)")


class LibraryError(Exception):
    """Съобщението е за човека."""


# --------------------------------------------------------------------------- настройка


def get_folder(posts_dir: Path) -> Path | None:
    """Избраната папка (posts/_settings.json), иначе WEB_FOLDER от .env, иначе няма."""
    raw = None
    try:
        raw = read_json(posts_dir / SETTINGS_FILE).get("folder")
    except (OSError, ValueError):
        pass
    raw = raw or os.environ.get("WEB_FOLDER", "").strip() or None
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def settings(posts_dir: Path) -> dict[str, Any]:
    try:
        return read_json(posts_dir / SETTINGS_FILE)
    except (OSError, ValueError):
        return {}


def save_settings(posts_dir: Path, values: dict[str, Any]) -> None:
    _save(posts_dir, values)


def set_folder(posts_dir: Path, raw: str | None) -> Path | None:
    if not raw:
        _save(posts_dir, {"folder": None})
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LibraryError("Посочете пълен път до папката (напр. C:\\Users\\...\\LinkedIn).")
    if not path.is_dir():
        raise LibraryError("Няма такава папка на този компютър — проверете пътя.")
    _save(posts_dir, {"folder": str(path)})
    return path


def _save(posts_dir: Path, values: dict[str, Any]) -> None:
    settings: dict[str, Any] = {}
    try:
        settings = read_json(posts_dir / SETTINGS_FILE)
    except (OSError, ValueError):
        pass
    settings.update(values)
    posts_dir.mkdir(parents=True, exist_ok=True)
    write_json(posts_dir / SETTINGS_FILE, settings)


# --------------------------------------------------------------------------- четене


@dataclass
class _Entry:
    rel: str
    path: Path


def _walk(root: Path) -> tuple[list[_Entry], list[_Entry]]:
    """(папки, файлове) до MAX_DEPTH нива, без скрити и без „LinkedIn постове" (наш изход)."""
    dirs: list[_Entry] = []
    files: list[_Entry] = []
    count = 0
    stack: list[tuple[Path, str, int]] = [(root, "", 0)]
    while stack:
        folder, prefix, depth = stack.pop()
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue  # без права / OneDrive файл само в облака — пропускаме тихо
        for child in children:
            count += 1
            if count > MAX_ENTRIES:
                return dirs, files
            if child.name.startswith((".", "~$")) or child.name == "LinkedIn постове":
                continue
            rel = f"{prefix}/{child.name}" if prefix else child.name
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if is_dir:
                dirs.append(_Entry(rel, child))
                if depth < MAX_DEPTH:
                    stack.append((child, rel, depth + 1))
            else:
                files.append(_Entry(rel, child))
    return dirs, files


def date_from_name(name: str) -> str | None:
    """„2026-10-20 среща" / „20.10 стажанти" → ISO дата; иначе None."""
    match = _LEADING_DATE.match(name)
    if not match:
        return None
    return schedule.parse_date(match.group(1).replace("_", "."))


def read_text_file(path: Path) -> str:
    """Текстов файл от Notepad/Word „Запиши като .txt" — UTF-8 (с или без BOM) или cp1251."""
    data = path.read_bytes()[: MAX_ALBUM_TEXT * 4]
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding).strip()[:MAX_ALBUM_TEXT]
        except UnicodeDecodeError:
            continue
    return ""


def _album_text(texts: list[_Entry]) -> tuple[str, str] | None:
    """Папка = пост: текстовият файл до снимките е брифът. Предпочита „бриф/текст/пост…"."""
    if not texts:
        return None
    texts = sorted(texts, key=lambda e: (not _TEXT_HINT.search(e.path.name), e.path.name.lower()))
    for entry in texts:
        try:
            body = read_text_file(entry.path)
        except OSError:
            continue
        if body:
            return entry.rel, body
    return None


def scan(folder: Path) -> dict[str, Any]:
    """
    Какво агентът вижда в папката — албуми със снимки и файлове с график.

    Подпапка със снимки И текстов файл (.txt/.md) е готов „пост": текстът е брифът му
    (какво се е случило), снимките — неговите. Така маркетингът подготвя постовете като
    обикновени папки, а агентът ги чете сам.
    """
    dirs, files = _walk(folder)
    by_dir: dict[str, list[_Entry]] = {}
    texts_by_dir: dict[str, list[_Entry]] = {}
    image_dirs = {e.rel.rsplit("/", 1)[0] for e in files if "/" in e.rel and e.path.suffix.lower() in IMAGE_SUFFIXES}
    schedules = []
    for entry in files:
        suffix = entry.path.suffix.lower()
        parent = entry.rel.rsplit("/", 1)[0] if "/" in entry.rel else ""
        if suffix in IMAGE_SUFFIXES:
            by_dir.setdefault(parent, []).append(entry)
        elif suffix in TEXT_SUFFIXES and parent and parent in image_dirs:
            texts_by_dir.setdefault(parent, []).append(entry)  # текстът на пост-папка, не график
        elif suffix in schedule.SUPPORTED and entry.rel.count("/") <= 1 and not entry.path.name.lower().startswith("readme"):
            score = (10 if _SCHEDULE_HINT.search(entry.path.name) else 0) + (3 if suffix in (".xlsx", ".csv", ".tsv") else 0)
            if score == 0 and suffix in (".md", ".txt", ".json"):
                continue  # бележки и README-та не са график, освен ако името не казва
            try:
                modified = entry.path.stat().st_mtime
            except OSError:
                modified = 0
            schedules.append({"path": entry.rel, "name": entry.path.name, "modified": round(modified), "score": score})
    schedules.sort(key=lambda s: (-s["score"], -s["modified"]))

    albums = []
    for rel, images in by_dir.items():
        name = rel.rsplit("/", 1)[-1] if rel else folder.name
        try:
            newest = max(img.path.stat().st_mtime for img in images)
        except OSError:
            newest = 0
        text = _album_text(texts_by_dir.get(rel, []))
        for entry in texts_by_dir.get(rel, []):
            try:
                newest = max(newest, entry.path.stat().st_mtime)  # променен текст = „нов" албум
            except OSError:
                pass
        albums.append({
            "path": rel,
            "name": name,
            "count": len(images),
            "date": date_from_name(name),
            "cover": images[0].rel,
            "modified": round(newest),
            "brief": text[1] if text else None,
            "brief_file": text[0] if text else None,
            "brief_hash": hashlib.sha1(text[1].encode("utf-8")).hexdigest()[:12] if text else None,
        })
    albums.sort(key=lambda a: (a["date"] or "", a["modified"]), reverse=True)
    return {
        "folder": str(folder),
        "name": folder.name,
        "albums": albums[:200],
        "schedules": schedules[:30],
        "dirs": len(dirs),
    }


def resolve(folder: Path, rel: str) -> Path:
    """Път вътре в папката; всичко, което излиза извън нея (.., абсолютен път), е отказ."""
    clean = (rel or "").replace("\\", "/").strip("/")
    target = (folder / clean).resolve()
    base = folder.resolve()
    if target != base and base not in target.parents:
        raise LibraryError("Този файл е извън избраната папка.")
    return target


def album_images(folder: Path, rel: str) -> list[Path]:
    """Снимките точно в тази подпапка (не по-дълбоко), подредени по име, до MAX_ALBUM_PHOTOS."""
    target = resolve(folder, rel)
    if not target.is_dir():
        raise LibraryError(f"Няма папка „{rel}“ в избраната папка.")
    try:
        images = [p for p in sorted(target.iterdir(), key=lambda p: p.name.lower())
                  if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
    except OSError as exc:
        raise LibraryError(f"Не мога да прочета „{rel}“.") from exc
    return images[:MAX_ALBUM_PHOTOS]


def resolve_photos(folder: Path | None, raw: list[str], when: str | None) -> list[Path]:
    """
    Колона „Снимки" от график → файлове. Същите правила като браузърния път в web-ui-2:
    подпапка или имена на файлове; празно → подпапка, чието име започва с датата на реда.
    """
    if folder is None:
        return []
    dirs, files = _walk(folder)
    dir_by_rel = {d.rel.lower(): d for d in dirs}
    file_by_rel = {f.rel.lower(): f for f in files if f.path.suffix.lower() in IMAGE_SUFFIXES}
    found: list[Path] = []

    def add_dir(rel: str) -> None:
        for image in album_images(folder, rel):
            if image not in found:
                found.append(image)

    for item in raw:
        norm = item.replace("\\", "/").strip().strip("/").lower()
        if not norm:
            continue
        if norm in dir_by_rel:
            add_dir(dir_by_rel[norm].rel)
            continue
        if norm in file_by_rel:
            found.append(file_by_rel[norm].path)
            continue
        base = norm.rsplit("/", 1)[-1]
        by_dir = [d for d in dirs if d.rel.lower().rsplit("/", 1)[-1] == base]
        if len(by_dir) == 1:
            add_dir(by_dir[0].rel)
            continue
        by_name = [f for f in file_by_rel.values() if f.path.name.lower() == base]
        if len(by_name) == 1:
            found.append(by_name[0].path)
    if not raw and when:
        matches = [d for d in dirs if date_from_name(d.rel.rsplit("/", 1)[-1]) == when]
        if len(matches) == 1:
            add_dir(matches[0].rel)
    return found[:MAX_ALBUM_PHOTOS]


# --------------------------------------------------------------------------- избор на папка


def browse(raw: str | None) -> dict[str, Any]:
    """Подпапките на дадена папка — за избора на папка в браузъра (само от този компютър)."""
    home = Path.home()
    path = Path(raw).expanduser() if raw else home
    if not path.is_dir():
        path = home
    try:
        children = sorted(
            (p for p in path.iterdir() if not p.name.startswith((".", "$")) and _is_dir(p)),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        children = []
    shortcuts = []
    for label, candidate in (
        ("Работен плот", _desktop(home)),
        ("Документи", home / "Documents"),
        ("Снимки", home / "Pictures"),
        ("Изтеглени", home / "Downloads"),
        ("Начало", home),
    ):
        if candidate and candidate.is_dir():
            shortcuts.append({"label": label, "path": str(candidate)})
    if os.name == "nt":  # „Този компютър" — всички дискове, не само домашната папка
        for letter in "CDEFGHIJ":
            drive = Path(f"{letter}:\\")
            if _is_dir(drive):
                shortcuts.append({"label": f"Диск {letter}:", "path": str(drive)})
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "parent": str(parent) if parent else None,
        "dirs": [{"name": p.name, "path": str(p)} for p in children[:300]],
        "shortcuts": shortcuts,
    }


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _desktop(home: Path) -> Path | None:
    """OneDrive често премества Работния плот — пробваме и там."""
    for candidate in [home / "Desktop", *sorted(home.glob("OneDrive*/Desktop"))]:
        if candidate.is_dir():
            return candidate
    return None


def thumbnail(source: Path, cache: Path, size: str = "s") -> Path | None:
    """JPEG миниатюра на снимка от папката — в кеша на агента; оригиналът не се пипа."""
    from src.web.uploads import THUMB_SIZES

    if not source.is_file() or source.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    edge = THUMB_SIZES.get(size, THUMB_SIZES["s"])
    stat = source.stat()
    key = hashlib.sha1(f"{source}|{stat.st_mtime}|{stat.st_size}|{size}".encode("utf-8")).hexdigest()[:20]
    target = cache / f"{key}.jpg"
    if target.exists():
        return target
    from PIL import Image, ImageOps

    cache.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((edge, edge))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=80)
    except (OSError, ValueError):
        return None
    target.write_bytes(buffer.getvalue())
    return target
