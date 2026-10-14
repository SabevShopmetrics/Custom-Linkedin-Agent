"""
Пакет = един качен график → много постове, пуснати паралелно (docs/technical/web-ui-2.md).

Файловата система остава състоянието: всеки пакет е posts/_batches/<id>.json (историята не
показва папки с „_"), а всеки ред от графика е обикновена папка на пост — същата като от чата,
отваря се и се ревизира там. В web.json на поста стоят `batch` и `scheduled_for`.

Опашката (кой чака ред) живее само в паметта на сървъра: след рестарт неготовите постове
се показват като „не е пуснат" и се пускат с един бутон.
"""

from __future__ import annotations

import csv
import io
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from src.pipeline.util import read_json, read_text_or_none, write_json

BATCHES_DIR = "_batches"
_BATCH_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


def batches_dir(posts_dir: Path) -> Path:
    return posts_dir / BATCHES_DIR


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]


def batch_path(posts_dir: Path, batch_id: str) -> Path | None:
    if not _BATCH_ID_RE.match(batch_id or ""):
        return None
    path = batches_dir(posts_dir) / f"{batch_id}.json"
    return path if path.is_file() else None


def save_batch(posts_dir: Path, batch: dict[str, Any]) -> None:
    folder = batches_dir(posts_dir)
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / f"{batch['id']}.json", batch)


def load_batch(posts_dir: Path, batch_id: str) -> dict[str, Any] | None:
    path = batch_path(posts_dir, batch_id)
    if path is None:
        return None
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def list_batches(posts_dir: Path) -> list[dict[str, Any]]:
    folder = batches_dir(posts_dir)
    if not folder.is_dir():
        return []
    items = []
    for path in folder.glob("*.json"):
        try:
            batch = read_json(path)
        except (OSError, ValueError):
            continue
        items.append(batch)
    items.sort(key=lambda b: b.get("created") or 0, reverse=True)
    return items


def final_text(post_dir: Path) -> str:
    """Текстът, който човекът вижда: ръчната редакция, ако има, иначе предложението на агента."""
    output = post_dir / "output"
    return ((read_text_or_none(output / "edited.md") or "").strip()
            or (read_text_or_none(output / "post.md") or "").strip())


def file_stem(index: int, item: dict[str, Any], title: str) -> str:
    """„03 2026-10-20 Среща на членовете" — подредено по дата във всяка файлова система."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)
    clean = " ".join(clean.split())[:70].rstrip(" .") or "пост"
    when = item.get("date") or "без-дата"
    return f"{index:02d} {when} {clean}"


def weekday(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        import datetime as _dt

        return _WEEKDAYS[_dt.date(y, m, d).weekday()]
    except (ValueError, TypeError):
        return None


def result_csv(rows: list[dict[str, Any]]) -> bytes:
    """Обобщение на пакета за Excel: „;" и UTF-8 с BOM — така българският Excel го отваря правилно."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Дата", "Заглавие", "Статус", "Думи", "Снимки", "Файл", "Публикуван", "Текст"])
    for row in rows:
        writer.writerow([
            row.get("date") or "",
            row.get("title") or "",
            row.get("status_label") or row.get("state") or "",
            row.get("words") or "",
            row.get("photo_count") or 0,
            row.get("file_name") or "",
            "да" if row.get("published") else "",
            row.get("text") or "",
        ])
    return buffer.getvalue().encode("utf-8-sig")


def export_zip(posts_dir: Path, rows: list[dict[str, Any]]) -> bytes:
    """Всички готови постове: .md с текста, избраните снимки в подпапка и обобщение в CSV."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            if not row.get("text"):
                continue
            stem = row["file_name"].rsplit(".", 1)[0]
            archive.writestr(row["file_name"], row["text"] + "\n")
            selected = posts_dir / row["post_id"] / "output" / "selected"
            for name in row.get("photos_in_post") or []:
                photo = selected / name
                if photo.is_file():
                    archive.write(photo, f"{stem}/{name}")
        archive.writestr("график-резултат.csv", result_csv(rows))
    return buffer.getvalue()


def make_batch(name: str, source: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": new_batch_id(),
        "name": " ".join((name or "").split())[:120] or "График",
        "source": (source or "")[:200] or None,
        "created": round(time.time()),
        "items": items,
    }
