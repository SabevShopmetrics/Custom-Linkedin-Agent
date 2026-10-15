"""
Стъпка 3 — Media Picker. Преглежда снимките в photos/, подрежда ги за поста и маркира рисковете.
Ако папката е празна — не е грешка (NoPhotosWarning), просто продължаваме само с текст.

Решение на потребителя 2026-10-13: снимките, които човекът е прикачил, са нарочно избрани —
ВСИЧКИ влизат в поста (до таван MAX_POST_PHOTOS, колкото позволява LinkedIn). Media Picker вече
не „избира най-добрите N", а:
  - подрежда ги (най-силната първа — тя се вижда най-голяма в лентата);
  - маркира topical_mismatch, както досега;
  - изключва САМО скрийншоти от компютър/телефон и почти еднакви дубликати, с причина.

Скорост (2026-10-13, на живо, 8 снимки): едно извикване с всички снимки и „мислене" отнемаше
218 s и изтичаше на таймаута; без „мислене" — 25 s, но пропускаше скрийншот на чат. Затова:
ЕДНО малко извикване на снимка, паралелно (NO_THINKING_ROLES=media в .env) — ~5 s за 8 снимки,
3 от 3 пускания × 23 снимки със същите решения като бавния вариант. Моделът само описва
снимката; подредбата (по strength) и дубликатите (перцептивен хеш) са правила в кода.
Снимка, чийто преглед се провали, ОСТАВА в поста — човекът я е прикачил нарочно.
Засичане на непълнолетни на снимките няма (махнато 2026-10-15 по решение на потребителя:
съгласията се одобряват предварително, а засичането даваше фалшиви „Пост иска преглед").
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.errors import ImageTooLargeError, InvalidResponseError, ModelUnavailableError, NoPhotosWarning
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import MediaExclusion, MediaSelectionItem, MediaSelectionSchema, PhotoKind, PhotoReviewSchema
from src.logging_utils import get_logger
from src.pipeline.util import list_photos, read_json, should_skip, write_json

MAX_POST_PHOTOS = 20  # LinkedIn приема до 20 снимки в един пост
# Едновременни заявки към сървъра на моделите. 8 паралелни бяха измерени на живо (2026-10-13)
# без забавяне на отделната заявка; повече снимки просто чакат свободно място.
MAX_PARALLEL_REVIEWS = 8
# Разлика в битове (от 64) между перцептивните хешове, под която две снимки са „същият кадър".
DUPLICATE_HASH_DISTANCE = 4

SYSTEM_PROMPT_TEMPLATE = """\
Ти си Media Picker в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Преглеждаш ЕДНА
снимка, която човекът е прикачил НАРОЧНО — по правило ВСИЧКИ влизат в поста. Името на файла
не значи нищо (често е „Screenshot …") — гледай само съдържанието.

"kind" — точно едно от:
- "screen_capture" — снимка на работен екран, където ОСНОВНОТО съдържание е интерфейс: чат или
  съобщения, код, терминал, настройки, админ панел, съобщение за грешка. Признаци: балончета на
  чат, бутони за реакции, редове код, прозорци и менюта на програма. Само тези НЕ влизат в поста
  — скрийншоти от компютър или телефон не са за публикуване, дори да споменават събитието.
- "graphic" — план или 3D визуализация на залата, карта, плакат, програма, брандирана графика.
- "photo" — реална снимка с хора или място.
План, карта или графика остава "graphic", дори да има един-два бутона или бял фон от уеб
страница около нея. При съмнение между "graphic" и "screen_capture" — избери "graphic".

"strength" 1–5: колко силна е като ПЪРВА снимка на поста (тя се вижда най-голяма в лентата):
хора в действие, ясно свързани с повода = 5; план или графика = 2–3; слаба или неясна = 1.

"topical_mismatch": true само при реално съмнение, че снимката е обща/стокова, без връзка с
конкретния повод от брифа; тогава кратка причина в "mismatch_reason". Снимката пак остава —
човекът решава.

Върни САМО JSON обект:
{{
  "kind": "photo" | "graphic" | "screen_capture",
  "description": "какво показва, до 20 думи",
  "strength": 1-5,
  "topical_mismatch": true/false,
  "mismatch_reason": "кратко защо не пасва, само ако topical_mismatch е true, иначе null"
}}
"""

_KIND_ORDER = {PhotoKind.PHOTO: 0, PhotoKind.GRAPHIC: 1, PhotoKind.SCREEN_CAPTURE: 2}


def _review_one(client: LLMClient, photo: Path, system: str, briefing_text: str) -> PhotoReviewSchema | None:
    """Една снимка, едно извикване. None = прегледът се провали (снимката остава, виж _assemble)."""
    prompt = f"Бриф за повода:\n{briefing_text}\n\nСнимка: {photo.name}"
    try:
        review = client.complete(Role.MEDIA, prompt, system=system, images=[photo], schema=PhotoReviewSchema)
    except (ModelUnavailableError, InvalidResponseError, ImageTooLargeError) as exc:
        # Грешен ключ (AuthenticationError) или спиране от човека НЕ се хващат — те важат за всичко.
        get_logger().warning("[3] не успях да прегледам %s: %s", photo.name, exc.human_message)
        return None
    assert isinstance(review, PhotoReviewSchema)
    return review


def _dhash(photo: Path) -> int | None:
    """64-битов разликов хеш (dHash) — еднакъв за един и същ кадър в различен размер/компресия."""
    try:
        from PIL import Image

        with Image.open(photo) as image:
            pixels = image.convert("L").resize((9, 8)).tobytes()  # 72 байта, по един на пиксел
    except Exception:  # noqa: BLE001 — нечетима снимка просто не участва в търсенето на дубликати
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return bits


def _assemble(photos: list[Path], reviews: list[PhotoReviewSchema | None]) -> MediaSelectionSchema:
    """
    Правилата в кода: скрийншотите вън; от почти еднакви кадри остава по-силният; останалите
    подредени по strength (снимки с хора преди графики), при равенство — по реда на качване.
    """
    excluded: list[MediaExclusion] = []
    kept: list[tuple[int, Path, PhotoReviewSchema | None]] = []
    for index, (photo, review) in enumerate(zip(photos, reviews)):
        if review is not None and review.kind == PhotoKind.SCREEN_CAPTURE:
            reason = "скрийншот от компютър/телефон" + (f" — {review.description}" if review.description else "")
            excluded.append(MediaExclusion(filename=photo.name, reason=reason))
        else:
            kept.append((index, photo, review))

    def rank(entry: tuple[int, Path, PhotoReviewSchema | None]) -> tuple[int, int, int]:
        index, _photo, review = entry
        if review is None:
            return (0, 1, index)  # непрегледана — след всички прегледани
        return (-review.strength, _KIND_ORDER[review.kind], index)

    kept.sort(key=rank)

    hashes: list[tuple[str, int]] = []
    selected: list[MediaSelectionItem] = []
    for _index, photo, review in kept:
        digest = _dhash(photo)
        twin = None
        if digest is not None:
            twin = next(
                (name for name, other in hashes if bin(digest ^ other).count("1") <= DUPLICATE_HASH_DISTANCE),
                None,
            )
        if twin is not None:
            # Подредбата е по сила, така че оцелява по-силният от двата кадъра.
            excluded.append(MediaExclusion(filename=photo.name, reason=f"почти еднакъв дубликат на {twin}"))
            continue
        if digest is not None:
            hashes.append((photo.name, digest))
        if review is None:
            selected.append(
                MediaSelectionItem(
                    filename=photo.name,
                    reason="прикачена от вас — моделът не успя да я прегледа",
                )
            )
        else:
            selected.append(
                MediaSelectionItem(
                    filename=photo.name,
                    reason=review.description or "прикачена от вас",
                    topical_mismatch=review.topical_mismatch,
                    mismatch_reason=review.mismatch_reason if review.topical_mismatch else None,
                )
            )
    return MediaSelectionSchema(selected=selected[:MAX_POST_PHOTOS], excluded=excluded)


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

    # Единствената роля с vision — снимката отива директно в заявката към модела (виж
    # llm/client.py::_build_messages и supports_vision в llm/roles.py).
    system = SYSTEM_PROMPT_TEMPLATE.format()
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_REVIEWS, len(photos))) as pool:
        reviews = list(pool.map(lambda photo: _review_one(client, photo, system, briefing_text), photos))

    selection = _assemble(photos, reviews)
    for item in selection.excluded:
        get_logger().info("[3] изключена снимка: %s — %s", item.filename, item.reason)
    write_json(media_path, selection.model_dump())
    return selection, False
