"""
Стъпка 3 — Media Picker. Преглежда снимките в photos/ и избира кои да влязат в поста.
Ако папката е празна — не е грешка (NoPhotosWarning), просто продължаваме само с текст.
"""

from __future__ import annotations

from pathlib import Path

from src.errors import NoPhotosWarning
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import MediaSelectionSchema
from src.logging_utils import get_logger
from src.pipeline.util import list_photos, read_json, should_skip, write_json

SYSTEM_PROMPT_TEMPLATE = """\
Ти си Media Picker в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Виждаш снимки
и трябва да избереш кои от тях да влязат в поста — максимум {max_photo_count}.

Предпочитай снимки, които показват хора в действие (не размазани, не дубликати на почти
същия кадър), и които съответстват на описанието в брифа.

ЗАДЪЛЖИТЕЛНО: за всяка избрана снимка прецени дали вероятно показва лице под 18 години
(ученическа възраст, униформа на гимназист и т.н.) и отбележи това с "possible_minor".
Това задейства задължителен човешки преглед преди публикуване — предпочитай да маркираш
при съмнение, не да пропуснеш.

Върни САМО JSON обект:
{{
  "selected": [
    {{
      "filename": "точното име на файла",
      "reason": "кратка причина защо е избрана",
      "possible_minor": true/false
    }}
  ]
}}

Ако нито една снимка не е подходяща, върни "selected": [].
"""


def run(
    post_dir: Path,
    client: LLMClient,
    plan,
    briefing_text: str,
    *,
    force: bool = False,
) -> tuple[MediaSelectionSchema, bool]:
    """
    Избира снимки от photos/ и записва media.json. Връща (избор, no_photos) — вторият
    елемент е True, когато папката е била празна, за да runner.py логне предупреждение
    без да препрочита файловата система.
    """
    media_path = post_dir / "media.json"
    photos = list_photos(post_dir / "photos")
    no_photos = len(photos) == 0

    if should_skip(media_path, force=force):
        # Възобновяемост: вече има избор от предишно пускане.
        return MediaSelectionSchema.model_validate(read_json(media_path)), no_photos

    if no_photos:
        # Празна папка не е грешка — просто няма какво да се избира, пропуска се LLM извикването.
        warning = NoPhotosWarning(str(post_dir))
        get_logger().info(warning.human_message)  # runner.py логва и на WARNING ниво в общия ход на run-а
        selection = MediaSelectionSchema(selected=[])
        write_json(media_path, selection.model_dump())
        return selection, True

    # Единствената роля с vision — снимките отиват директно в заявката към модела (виж
    # llm/client.py::_build_messages и supports_vision в llm/roles.py).
    system = SYSTEM_PROMPT_TEMPLATE.format(max_photo_count=plan.max_photo_count)
    prompt = (
        f"Бриф за повода:\n{briefing_text}\n\n"
        f"Налични снимки: {', '.join(p.name for p in photos)}"
    )

    selection = client.complete(Role.MEDIA, prompt, system=system, images=photos, schema=MediaSelectionSchema)
    assert isinstance(selection, MediaSelectionSchema)
    write_json(media_path, selection.model_dump())
    return selection, False
