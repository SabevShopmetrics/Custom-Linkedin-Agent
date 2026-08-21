"""Дребни файлови помощници, споделени между стъпките на конвейера."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# .jfif е честo разширение при сваляне на снимки от браузър (Chrome/Edge от Google/Bing
# Images) — самите байтове са обикновен JPEG, само разширението е различно.
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".jfif"}


def read_text_or_none(path: Path) -> str | None:
    """Съдържанието на файл, или None ако липсва — вместо изключение при всяка проверка."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    """Записва текст в UTF-8, създавайки родителските папки при нужда."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, data: dict[str, Any]) -> None:
    """JSON, четим за човек (indent=2) и с кирилица дословно (ensure_ascii=False)."""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path) -> dict[str, Any]:
    """Обратното на write_json — очаква файлът да съществува (за разлика от read_text_or_none)."""
    return json.loads(path.read_text(encoding="utf-8"))


def list_photos(photos_dir: Path) -> list[Path]:
    """Снимките в photos/ на един пост, по азбучен ред — Media Picker разчита на реда."""
    if not photos_dir.is_dir():
        return []
    return sorted(p for p in photos_dir.iterdir() if p.suffix.lower() in PHOTO_EXTENSIONS)


def should_skip(output_path: Path, *, force: bool) -> bool:
    """Спазва правилото за възобновяемост: пропусни стъпка, чийто изходен файл вече съществува."""
    return output_path.exists() and not force


def file_uri(path: Path) -> str:
    """
    `file://` линк към локален път — кликаем в повечето терминали (Windows Terminal, iTerm,
    VS Code) за отваряне без copy-paste. `Path.as_uri()` изисква абсолютен път, затова
    `resolve()` първо.
    """
    return path.resolve().as_uri()
