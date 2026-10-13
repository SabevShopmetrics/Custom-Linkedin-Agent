"""
Снимки за уеб интерфейса: качване на отделни снимки и ZIP архиви, безопасно разархивиране,
включване/изключване от това, което агентът вижда, и миниатюри за мрежата в браузъра.

Файловата система остава единственото състояние (както навсякъде в проекта):

  posts/<пост>/photos/       — снимките, които агентът (Media Picker) вижда и от които избира
  posts/<пост>/photos_off/   — качени, но изключени от човека; агентът не ги вижда
  posts/<пост>/.thumbs/      — кеш с миниатюри (може да се изтрие по всяко време)

Изключването е преместване между двете папки, не изтриване — нищо качено не се губи.

Защо таван на включените снимки (MAX_AGENT_PHOTOS): Media Picker праща ВСИЧКИ снимки от
photos/ към vision модела в една заявка. ZIP от събитие лесно носи 60+ снимки — това е
многократно по-бавно и рискува таймаут. Над тавана качените снимки отиват в photos_off/ и
човекът сам избира кои да размени.
"""

from __future__ import annotations

import io
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from src.pipeline.util import PHOTO_EXTENSIONS, read_json, write_json

INCLUDED_DIR = "photos"
EXCLUDED_DIR = "photos_off"
THUMBS_DIR = ".thumbs"

MAX_AGENT_PHOTOS = 12
MAX_IMAGE_BYTES = 60 * 1024 * 1024  # една снимка — RAW/панорами над това не са за LinkedIn
MAX_ZIP_ENTRIES = 2000
MAX_ZIP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # сумата на разархивираното — защита от „zip бомба"

THUMB_SIZES = {"s": 360, "m": 1080}

_HEIC_SUFFIXES = {".heic", ".heif"}


@dataclass
class UploadReport:
    """Какво стана с едно качване — показва се на човека дословно."""

    added: list[str] = field(default_factory=list)  # включени (агентът ще ги види)
    parked: list[str] = field(default_factory=list)  # качени, но изключени заради тавана
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (име, причина)

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "parked": self.parked,
            "skipped": [{"name": name, "reason": reason} for name, reason in self.skipped],
        }


# --------------------------------------------------------------------------- имена


def safe_filename(raw: str) -> str:
    """
    Име на файл, безопасно за диска: само последната част на пътя (никакво „../"), NFC
    (Media Picker сравнява имена в NFC — виж consolidator._resolve_photo_path), без
    управляващи и запазени за Windows знаци. Кирилицата остава.
    """
    base = re.split(r"[\\/]", raw or "")[-1]
    base = unicodedata.normalize("NFC", base).strip().strip(".")
    base = re.sub(r'[\x00-\x1f<>:"|?*]', "_", base)
    base = re.sub(r"\s+", " ", base)
    stem, dot, suffix = base.rpartition(".")
    if not dot:
        stem, suffix = base, ""
    stem = stem[:80].strip() or "snimka"
    return f"{stem}.{suffix.lower()}" if suffix else stem


def _all_names(post_dir: Path) -> set[str]:
    names: set[str] = set()
    for folder in (INCLUDED_DIR, EXCLUDED_DIR):
        directory = post_dir / folder
        if directory.is_dir():
            names.update(p.name.lower() for p in directory.iterdir())
    return names


def _unique_name(post_dir: Path, name: str) -> str:
    """Имената са уникални в двете папки заедно — превключването не бива да презаписва."""
    taken = _all_names(post_dir)
    if name.lower() not in taken:
        return name
    stem, _, suffix = name.rpartition(".")
    counter = 2
    while f"{stem}-{counter}.{suffix}".lower() in taken:
        counter += 1
    return f"{stem}-{counter}.{suffix}"


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """
    ZIP, направен с „Изпрати към → Компресирана папка" на Windows, записва кирилските имена
    в OEM кодировка (cp866), без UTF-8 флага — zipfile ги чете като cp437 и излизат като
    „ÆÑ¿". Опитваме да ги върнем; при неуспех оставяме каквото zipfile е прочел.
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for encoding in ("utf-8", "cp866"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


# --------------------------------------------------------------------------- списък / състояние


def find_photo(post_dir: Path, name: str) -> Path | None:
    """Точно съвпадение по име в двете папки — потребителски вход никога не се слепва в път."""
    target = unicodedata.normalize("NFC", name)
    for folder in (INCLUDED_DIR, EXCLUDED_DIR):
        directory = post_dir / folder
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if candidate.is_file() and unicodedata.normalize("NFC", candidate.name) == target:
                return candidate
    return None


def list_photo_entries(post_dir: Path) -> list[dict]:
    """Всички качени снимки по азбучен ред, с флаг дали агентът ги вижда."""
    entries: list[dict] = []
    for folder, included in ((INCLUDED_DIR, True), (EXCLUDED_DIR, False)):
        directory = post_dir / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS:
                entries.append({"name": path.name, "included": included, "bytes": path.stat().st_size})
    entries.sort(key=lambda e: e["name"].lower())
    return entries


def included_count(post_dir: Path) -> int:
    return sum(1 for entry in list_photo_entries(post_dir) if entry["included"])


# --------------------------------------------------------------------------- качване


def _is_image(data_source: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(data_source) as image:
            image.verify()
        return True
    except Exception:
        return False


def _accept_image(post_dir: Path, display_name: str, source: Path, report: UploadReport) -> None:
    """Проверява, че файлът е истинска снимка, и го слага в photos/ (или photos_off/ над тавана)."""
    name = safe_filename(display_name)
    if Path(name).suffix.lower() not in PHOTO_EXTENSIONS:
        report.skipped.append((display_name, "не е снимка (JPG, PNG, WEBP)"))
        return
    if source.stat().st_size > MAX_IMAGE_BYTES:
        report.skipped.append((display_name, "твърде голям файл за снимка (над 60 MB)"))
        return
    if not _is_image(source):
        report.skipped.append((display_name, "файлът не се отваря като снимка (повреден?)"))
        return

    name = _unique_name(post_dir, name)
    room = included_count(post_dir) < MAX_AGENT_PHOTOS
    folder = post_dir / (INCLUDED_DIR if room else EXCLUDED_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), folder / name)
    (report.added if room else report.parked).append(name)


def _extract_zip(post_dir: Path, archive: Path, scratch: Path, report: UploadReport) -> None:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        report.skipped.append((archive.name, "ZIP архивът е повреден или непълен"))
        return

    with zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        if len(members) > MAX_ZIP_ENTRIES:
            report.skipped.append((archive.name, f"архивът има над {MAX_ZIP_ENTRIES} файла"))
            return
        if sum(info.file_size for info in members) > MAX_ZIP_TOTAL_BYTES:
            report.skipped.append((archive.name, "архивът е твърде голям след разархивиране"))
            return

        # Подредба по пътя в архива — снимките от едно събитие обикновено са номерирани.
        for info in sorted(members, key=lambda i: _decode_zip_name(i).lower()):
            full_name = _decode_zip_name(info)
            parts = re.split(r"[\\/]", full_name)
            leaf = parts[-1]
            # Служебни файлове на macOS/Windows — не са снимки на човека, не ги споменаваме.
            if "__MACOSX" in parts or leaf.startswith(".") or leaf.lower() in {"thumbs.db", "desktop.ini"}:
                continue
            suffix = Path(leaf).suffix.lower()
            if suffix in _HEIC_SUFFIXES:
                report.skipped.append((leaf, _HEIC_REASON))
                continue
            if suffix == ".zip":
                report.skipped.append((leaf, "архив в архива — разархивирайте го отделно"))
                continue
            if suffix not in PHOTO_EXTENSIONS:
                report.skipped.append((leaf, "не е снимка"))
                continue
            if info.flag_bits & 0x1:
                report.skipped.append((leaf, "защитен с парола"))
                continue
            if info.file_size > MAX_IMAGE_BYTES:
                report.skipped.append((leaf, "твърде голям файл за снимка (над 60 MB)"))
                continue

            target = scratch / f"z-{len(report.added) + len(report.parked) + len(report.skipped)}{suffix}"
            try:
                with zf.open(info) as src, target.open("wb") as dst:
                    # Размерът в заглавката може да лъже — копираме с таван, не на доверие.
                    copied = 0
                    while chunk := src.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > MAX_IMAGE_BYTES:
                            raise ValueError("over limit")
                        dst.write(chunk)
            except (zipfile.BadZipFile, ValueError, OSError, RuntimeError, NotImplementedError):
                target.unlink(missing_ok=True)
                report.skipped.append((leaf, "не можа да се разархивира"))
                continue
            _accept_image(post_dir, leaf, target, report)
            target.unlink(missing_ok=True)  # остава само ако снимката е отхвърлена


_HEIC_REASON = (
    "HEIC (iPhone) не се поддържа — изпратете снимката като JPG или на iPhone: "
    "Настройки → Камера → Формати → „Най-съвместим“"
)


def add_upload(post_dir: Path, original_name: str, temp_file: Path) -> UploadReport:
    """
    Обработва един качен файл (вече записан на диска в temp_file): снимка → photos/,
    ZIP → разархивира снимките вътре. temp_file се премахва накрая при всички случаи.
    """
    report = UploadReport()
    scratch = temp_file.parent
    suffix = Path(original_name).suffix.lower()
    try:
        if suffix == ".zip" or (suffix not in PHOTO_EXTENSIONS and zipfile.is_zipfile(temp_file)):
            _extract_zip(post_dir, temp_file, scratch, report)
        elif suffix in _HEIC_SUFFIXES:
            report.skipped.append((original_name, _HEIC_REASON))
        elif suffix in PHOTO_EXTENSIONS:
            _accept_image(post_dir, original_name, temp_file, report)
        else:
            report.skipped.append((original_name, "не е снимка или ZIP архив"))
    finally:
        temp_file.unlink(missing_ok=True)
    return report


# --------------------------------------------------------------------------- включване / избор


def copy_in(post_dir: Path, source: Path) -> str:
    """Копира снимка от друг пост в този (включена — човекът изрично я е прикачил)."""
    name = _unique_name(post_dir, safe_filename(source.name))
    folder = post_dir / INCLUDED_DIR
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, folder / name)
    return name



def set_included(post_dir: Path, name: str, included: bool) -> bool:
    """Мести снимката между photos/ и photos_off/. False — няма такава снимка."""
    path = find_photo(post_dir, name)
    if path is None:
        return False
    target_dir = post_dir / (INCLUDED_DIR if included else EXCLUDED_DIR)
    if path.parent == target_dir:
        return True
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), target_dir / path.name)
    if not included:
        current = selected_photos(post_dir)
        if path.name in current:
            set_selection(post_dir, [n for n in current if n != path.name])
    return True


def selected_photos(post_dir: Path) -> list[str]:
    """Снимките в готовия пост, в реда им — от meta.json (там е и ръчната подредба)."""
    meta_path = post_dir / "output" / "meta.json"
    try:
        names = (read_json(meta_path).get("photos") or {}).get("selected") or []
    except (OSError, ValueError):
        names = []
    selected_dir = post_dir / "output" / "selected"
    return [n for n in names if (selected_dir / n).is_file()]


def set_selection(post_dir: Path, names: list[str]) -> list[str]:
    """
    Ръчен избор на снимките в поста (човекът има последната дума над Media Picker).
    Пренаписва output/selected/ (копия, възстановими от photos/) и photos.selected в
    meta.json, с отметка human_override. Приема само включени снимки; непознатите се пропускат.
    """
    output_dir = post_dir / "output"
    selected_dir = output_dir / "selected"
    selected_dir.mkdir(parents=True, exist_ok=True)

    chosen: list[str] = []
    for name in names:
        path = find_photo(post_dir, name)
        if path is None or path.parent.name != INCLUDED_DIR or path.name in chosen:
            continue
        chosen.append(path.name)

    for old in selected_dir.iterdir():
        if old.is_file() and old.name not in chosen:
            old.unlink()
    for name in chosen:
        if not (selected_dir / name).exists():
            shutil.copy2(post_dir / INCLUDED_DIR / name, selected_dir / name)

    meta_path = output_dir / "meta.json"
    try:
        meta = read_json(meta_path)
    except (OSError, ValueError):
        meta = {}
    photos = dict(meta.get("photos") or {})
    photos["selected"] = chosen
    photos["human_override"] = True
    meta["photos"] = photos
    write_json(meta_path, meta)
    return chosen


# --------------------------------------------------------------------------- миниатюри


def thumbnail(post_dir: Path, name: str, size: str = "s") -> Path | None:
    """JPEG миниатюра от кеша (.thumbs/), създадена при нужда. None — няма такава снимка."""
    source = find_photo(post_dir, name)
    if source is None:
        return None
    edge = THUMB_SIZES.get(size, THUMB_SIZES["s"])
    thumbs = post_dir / THUMBS_DIR
    target = thumbs / f"{source.name}.{size}.jpg"
    if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target

    from PIL import Image, ImageOps

    thumbs.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)  # снимки от телефон: иначе излизат легнали
        image.thumbnail((edge, edge))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=82)
    target.write_bytes(buffer.getvalue())
    return target
