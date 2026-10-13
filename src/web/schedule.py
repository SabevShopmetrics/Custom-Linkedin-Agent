"""
График → списък от постове (docs/technical/web-ui-2.md).

Човекът качва (или посочва в своя папка) график — таблица или текст — и всеки ред става
отделен пост, пуснат паралелно. Приемаме това, което маркетингът реално има подръка:

  .csv / .tsv / .txt с разделители  — от Excel „Запиши като CSV" (; или , или табулация)
  .xlsx                             — директно от Excel (чете се първият лист, без библиотеки)
  .txt / .md без колони              — по един пост на блок, блоковете са разделени с ред „---"
                                      (или с празен ред, ако няма „---"); дата в началото по желание
  .json                             — списък от обекти със същите полета

Колоните се разпознават по заглавие (BG/EN, без значение главни/малки букви):
  дата | date | ден          — кога е планиран постът (по желание)
  бриф | тема | текст | описание | brief | topic   — какво се е случило (задължително)
  режим | дължина | mode     — кадър / фокус / панорама (по желание)
  снимки | папка | photos    — подпапка или имена на файлове в папката на човека (по желание)
  заглавие | title           — етикет в историята (по желание)
Ако няма разпознат ред със заглавия, първата колона с дата е дата, а най-дългата — бриф.

Само стандартна библиотека — виж server.py защо.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from xml.etree import ElementTree

from src.pipeline import modes

MAX_ROWS = 60  # един график = до 60 поста; повече е по-скоро грешен файл, отколкото план
MAX_BRIEF_CHARS = 6000
SUPPORTED = (".csv", ".tsv", ".txt", ".md", ".xlsx", ".json")


class ScheduleError(Exception):
    """Графикът не може да се прочете — съобщението е за човека."""


@dataclass
class ScheduleRow:
    row: int  # номер на реда в източника (за съобщения „ред 7: …")
    brief: str
    date: str | None = None  # ISO, YYYY-MM-DD
    mode: str | None = None
    photos: list[str] = field(default_factory=list)
    title: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedSchedule:
    rows: list[ScheduleRow]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {"rows": [r.to_dict() for r in self.rows], "warnings": self.warnings}


# --------------------------------------------------------------------------- колони

_COLUMNS = {
    "date": ("дата", "date", "ден", "кога", "day", "публикуване", "дата на публикуване"),
    "brief": ("бриф", "тема", "текст", "описание", "съдържание", "brief", "topic", "text", "description", "какво"),
    "mode": ("режим", "дължина", "mode", "length", "вид"),
    "photos": ("снимки", "снимка", "папка", "photos", "photo", "images", "folder", "файлове"),
    "title": ("заглавие", "title", "име", "name"),
}


def _column_kind(header: str) -> str | None:
    clean = re.sub(r"[^\w ]+", " ", (header or "").strip().lower()).strip()
    if not clean:
        return None
    for kind, names in _COLUMNS.items():
        if clean in names:
            return kind
    for kind, names in _COLUMNS.items():  # „Дата на поста", „Тема / бриф"
        if any(clean.startswith(name) for name in names if len(name) > 2):
            return kind
    return None


# --------------------------------------------------------------------------- дати

_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})"), ("d", "m", "y")),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2})(?!\d)"), ("d", "m", "y2")),
)
_MONTHS = {
    "яну": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "юни": 6, "юли": 7, "авг": 8, "сеп": 9, "окт": 10, "ное": 11,
    "дек": 12,
}
_SHORT_DATE = re.compile(r"^(\d{1,2})[./](\d{1,2})\.?\s*$")  # само цялата клетка — „3.5 милиона" не е дата
_WORD_DATE = re.compile(r"^(\d{1,2})\s+([а-я]{3})[а-я]*\.?\s*(\d{4})?", re.IGNORECASE)


def parse_date(raw: object, *, today: date | None = None) -> str | None:
    """„20.10.2026", „2026-10-20", „20 октомври", Excel число → ISO. Непознато → None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and 30000 < float(raw) < 80000:
        return (date(1899, 12, 30) + timedelta(days=int(float(raw)))).isoformat()  # сериен номер на Excel
    text = str(raw).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", text):
        return parse_date(float(text), today=today)
    for pattern, order in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        parts = dict(zip(order, (int(g) for g in match.groups())))
        year = parts.get("y") or 2000 + parts.get("y2", 0)
        try:
            return date(year, parts["m"], parts["d"]).isoformat()
        except ValueError:
            return None
    match = _SHORT_DATE.match(text)
    if match:  # „03.11" — без година: най-близката такава дата напред (графикът е за бъдещето)
        return _nearest(int(match.group(1)), int(match.group(2)), today)
    match = _WORD_DATE.match(text.lower())
    if match and match.group(2) in _MONTHS:
        if not match.group(3):
            return _nearest(int(match.group(1)), _MONTHS[match.group(2)], today)
        try:
            return date(int(match.group(3)), _MONTHS[match.group(2)], int(match.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _nearest(day: int, month: int, today: date | None) -> str | None:
    today = today or date.today()
    try:
        candidate = date(today.year, month, day)
        if (today - candidate).days > 31:  # отдавна мина → догодина (декември → януари)
            candidate = date(today.year + 1, month, day)
    except ValueError:
        return None
    return candidate.isoformat()


# --------------------------------------------------------------------------- редове → постове


def _split_photos(raw: str) -> list[str]:
    parts = re.split(r"[;,\n]+", raw or "")
    return [p.strip().strip('"').strip() for p in parts if p.strip().strip('"').strip()][:50]


def _rows_from_table(table: list[list[str]], warnings: list[str]) -> list[ScheduleRow]:
    table = [[(c or "").strip() for c in row] for row in table]
    table = [row for row in table if any(row)]
    if not table:
        return []
    header = table[0]
    kinds = [_column_kind(h) for h in header]
    if "brief" in kinds:
        body = list(enumerate(table[1:], start=2))
    else:
        # Без заглавия: дата е първата колона, която прилича на дата; бриф — най-дългата.
        body = list(enumerate(table, start=1))
        width = max(len(r) for r in table)
        kinds = [None] * width
        lengths = [sum(len(r[i]) for r in table if i < len(r)) for i in range(width)]
        for i in range(width):
            if all(parse_date(r[i]) for r in table if i < len(r) and r[i]):
                kinds[i] = "date"
                break
        candidates = [i for i in range(width) if kinds[i] is None]
        if not candidates:
            return []
        kinds[max(candidates, key=lambda i: lengths[i])] = "brief"
        warnings.append("Няма ред със заглавия на колоните — приех най-дългата колона за бриф.")

    rows: list[ScheduleRow] = []
    for number, cells in body:
        values: dict[str, str] = {}
        for index, kind in enumerate(kinds):
            if kind and index < len(cells) and cells[index] and kind not in values:
                values[kind] = cells[index]
        row = _make_row(number, values, warnings)
        if row is not None:
            rows.append(row)
    return rows


def _make_row(number: int, values: dict[str, str], warnings: list[str]) -> ScheduleRow | None:
    brief = (values.get("brief") or "").strip()
    if not brief:
        if any(values.values()):
            warnings.append(f"Ред {number}: няма бриф — пропуснат.")
        return None
    if len(brief) > MAX_BRIEF_CHARS:
        brief = brief[:MAX_BRIEF_CHARS]
        warnings.append(f"Ред {number}: брифът е много дълъг — съкратен до {MAX_BRIEF_CHARS} знака.")
    when = parse_date(values.get("date"))
    if values.get("date") and not when:
        warnings.append(f"Ред {number}: не разпознах датата „{values['date']}“ — постът е без дата.")
    mode = None
    if values.get("mode"):
        mode = modes.resolve_mode(values["mode"].strip().lower())
        if not mode:
            warnings.append(f"Ред {number}: непознат режим „{values['mode']}“ — ползвам подразбирането.")
    return ScheduleRow(
        row=number,
        brief=brief,
        date=when,
        mode=mode,
        photos=_split_photos(values.get("photos") or ""),
        title=(values.get("title") or "").strip()[:120] or None,
    )


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):  # Excel на български Windows пише CSV в cp1251
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _looks_tabular(text: str) -> str | None:
    lines = [line for line in text.splitlines() if line.strip()][:10]
    if len(lines) < 1:
        return None
    for delimiter in ("\t", ";", ","):
        counts = [line.count(delimiter) for line in lines]
        if counts[0] >= 1 and _column_kind_line(lines[0], delimiter):
            return delimiter
        if len(lines) >= 2 and min(counts) >= 1 and len(set(counts)) == 1 and delimiter != ",":
            return delimiter
    return None


def _column_kind_line(line: str, delimiter: str) -> bool:
    cells = next(csv.reader([line], delimiter=delimiter))
    return any(_column_kind(cell) == "brief" for cell in cells)


_LEADING_DATE = re.compile(
    r"^\s*(?:[#*>-]\s*)*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{1,2}[./]\d{1,2}\.?(?=\s*[:\-–—|])|\d{1,2}\s+[а-яА-Я]+\.?(?:\s+\d{4})?)"
    r"\s*(?:г\.)?\s*[:\-–—|]?\s*"
)


def _rows_from_text(text: str, warnings: list[str]) -> list[ScheduleRow]:
    lines = text.replace("\r\n", "\n").split("\n")
    if any(re.fullmatch(r"\s*-{3,}\s*", line) for line in lines):
        blocks = re.split(r"\n\s*-{3,}\s*\n", "\n" + text.replace("\r\n", "\n") + "\n")
    else:
        blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    rows: list[ScheduleRow] = []
    for number, block in enumerate(blocks, start=1):
        block = block.strip()
        if not block:
            continue
        values: dict[str, str] = {}
        match = _LEADING_DATE.match(block)
        if match and parse_date(match.group(1)):
            values["date"] = match.group(1)
            block = block[match.end():].strip()
        kept: list[str] = []
        for line in block.split("\n"):  # „Снимки: папка" / „Режим: кадър" на отделен ред
            key, sep, value = line.partition(":")
            kind = _column_kind(key) if sep and len(key) < 25 else None
            if kind in ("photos", "mode", "title", "date") and value.strip():
                values[kind] = value.strip()
            else:
                kept.append(line)
        values["brief"] = "\n".join(kept).strip()
        row = _make_row(len(rows) + 1, values, warnings)
        if row is not None:
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- xlsx (без библиотеки)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _xlsx_table(data: bytes) -> list[list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ScheduleError("Файлът не е истински Excel (.xlsx). Запишете го наново от Excel или като CSV.") from exc
    with archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in item.iter(f"{{{_NS['m']}}}t")))
        sheet = _first_sheet_path(archive, names)
        date_styles = _date_styles(archive, names)
        root = ElementTree.fromstring(archive.read(sheet))
    table: list[list[str]] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, str] = {}
        for cell in row.findall("m:c", _NS):
            column = _column_index(cell.get("r") or "")
            kind = cell.get("t")
            value = cell.find("m:v", _NS)
            raw = value.text if value is not None else None
            if kind == "s" and raw is not None:
                text = shared[int(raw)] if int(raw) < len(shared) else ""
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
            elif raw is not None and cell.get("s") in date_styles:
                text = parse_date(float(raw)) or raw
            else:
                text = raw or ""
            if column is None:
                column = len(cells)
            cells[column] = text
        if cells:
            width = max(cells) + 1
            table.append([cells.get(i, "") for i in range(width)])
    return table


def _first_sheet_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    try:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        first = workbook.find("m:sheets/m:sheet", _NS)
        target_id = first.get(_REL_NS) if first is not None else None
        for rel in rels:
            if rel.get("Id") == target_id:
                target = rel.get("Target", "").lstrip("/")
                path = target if target.startswith("xl/") else f"xl/{target}"
                if path in names:
                    return path
    except (KeyError, ElementTree.ParseError):
        pass
    sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheets:
        raise ScheduleError("В Excel файла няма лист с данни.")
    return sheets[0]


def _date_styles(archive: zipfile.ZipFile, names: set[str]) -> set[str]:
    """Индекси на стилове с формат за дата — за да не излезе 46315 вместо 20.10.2026."""
    if "xl/styles.xml" not in names:
        return set()
    try:
        root = ElementTree.fromstring(archive.read("xl/styles.xml"))
    except ElementTree.ParseError:
        return set()
    custom = {
        fmt.get("numFmtId"): (fmt.get("formatCode") or "").lower()
        for fmt in root.findall("m:numFmts/m:numFmt", _NS)
    }
    builtin_dates = {str(i) for i in (*range(14, 23), *range(45, 48))}
    result: set[str] = set()
    for index, xf in enumerate(root.findall("m:cellXfs/m:xf", _NS)):
        fmt_id = xf.get("numFmtId") or "0"
        code = custom.get(fmt_id, "")
        if fmt_id in builtin_dates or (code and re.search(r"[dy]", re.sub(r'"[^"]*"|\[[^]]*]', "", code))):
            result.add(str(index))
    return result


def _column_index(ref: str) -> int | None:
    letters = re.match(r"[A-Z]+", ref)
    if not letters:
        return None
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - 64)
    return index - 1


# --------------------------------------------------------------------------- вход


def parse_schedule(name: str, data: bytes) -> ParsedSchedule:
    """Разпознава формата по разширението (и съдържанието) и връща редовете."""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".txt"
    warnings: list[str] = []
    if suffix in (".xls", ".ods", ".numbers", ".docx", ".doc", ".pdf"):
        raise ScheduleError(
            f"Форматът {suffix} не се чете. Запишете графика като .xlsx или .csv (Excel: „Запиши като“)."
        )
    if suffix == ".xlsx":
        rows = _rows_from_table(_xlsx_table(data), warnings)
    elif suffix == ".json":
        rows = _rows_from_json(data, warnings)
    else:
        text = _decode(data)
        delimiter = "\t" if suffix == ".tsv" else _looks_tabular(text)
        if suffix == ".csv" and delimiter is None:
            delimiter = ";" if text.count(";") > text.count(",") else ","
        if delimiter:
            rows = _rows_from_table(list(csv.reader(io.StringIO(text), delimiter=delimiter)), warnings)
        else:
            rows = _rows_from_text(text, warnings)
    if not rows:
        raise ScheduleError(
            "Не намерих нито един пост в графика. Нужна е колона „Бриф“ (или „Тема“) — по един ред на пост; "
            "или текст с по един пост на абзац."
        )
    if len(rows) > MAX_ROWS:
        warnings.append(f"Графикът има {len(rows)} поста — взимам първите {MAX_ROWS}.")
        rows = rows[:MAX_ROWS]
    rows.sort(key=lambda r: (r.date is None, r.date or "", r.row))
    return ParsedSchedule(rows=rows, warnings=warnings)


def _rows_from_json(data: bytes, warnings: list[str]) -> list[ScheduleRow]:
    try:
        items = json.loads(_decode(data))
    except ValueError as exc:
        raise ScheduleError("JSON файлът не се чете — проверете запетаите и кавичките.") from exc
    if isinstance(items, dict):
        items = items.get("posts") or items.get("rows") or []
    rows: list[ScheduleRow] = []
    for number, item in enumerate(items if isinstance(items, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        values: dict[str, str] = {}
        for key, value in item.items():
            kind = _column_kind(str(key))
            if kind and value not in (None, ""):
                values[kind] = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
        row = _make_row(number, values, warnings)
        if row is not None:
            rows.append(row)
    return rows


def rows_from_payload(items: list[dict]) -> list[ScheduleRow]:
    """Редовете, както ги връща браузърът след преглед (човекът може да ги е поправил)."""
    rows: list[ScheduleRow] = []
    for number, item in enumerate(items[:MAX_ROWS], start=1):
        if not isinstance(item, dict):
            continue
        brief = str(item.get("brief") or "").strip()[:MAX_BRIEF_CHARS]
        if not brief:
            continue
        photos = item.get("photos")
        rows.append(ScheduleRow(
            row=int(item.get("row") or number),
            brief=brief,
            date=parse_date(item.get("date")) if item.get("date") else None,
            mode=modes.resolve_mode(item.get("mode")) if item.get("mode") else None,
            photos=[str(p)[:300] for p in photos][:50] if isinstance(photos, list) else [],
            title=str(item.get("title") or "").strip()[:120] or None,
        ))
    return rows


def today_iso() -> str:
    return datetime.now().date().isoformat()
