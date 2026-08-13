"""
Оркестрира конвейера и управлява възобновяемостта.

Верига (от 2026-08-11, потребителска инструкция — виж docs/technical/process-3.md):

  [1] Планер
  [2] Писател → post_i.md        ┐ паралелно, независими
  [3] Media Picker → media.json  ┘
  [4] Критик върху Post I → critique.md — намира слабостите
  [5] Писател → post_ii.md — ВТОРИ пост, който запълва тези слабости
  [6] Консолидатор → output/post.md — официалното предложение ОТ ДВАТА поста
  [7] Критик върху предложението → critique_final.md — само констатира какво остава открито

Какво се смени и защо: дотогава Критикът беше вето. Присъда needs_revision пращаше
Писателя на до 2 опита да пренапише същия текст, а после конвейерът спираше с needs_human
и хвърляше NeedsHumanReviewError; нарушение на rules/forbidden.md спираше веднага. На
практика това значеше, че при строг Критик човекът не получаваше нищо — при реален тест
на 2026-08-11 три поредни кръга свършиха с "отхвърлен", без предложение на изхода.

Сега Критикът не отхвърля. Находките му са дневен ред за втори пост, а Консолидаторът
съставя официалното предложение от двата — по-силната формулировка за всяко твърдение.
Конвейерът ВИНАГИ стига до предложение. Статусът казва само дали остава нещо открито:

  "ready"            — нищо открито след финалната проверка
  "ready_with_notes" — има открити въпроси (тон, недоказани твърдения, правила) и/или
                       предупреждения за снимки; предложението пак е налично

Никоя присъда на Критика вече не хвърля изключение. Изключения остават само за вход и
инфраструктура: липсващ/нередактиран бриф (преди LLM извикванията) и недостъпен модел.
Стъпки 5, 6 и 7 са допълнително подсигурени — ако някоя от тях се счупи, конвейерът
записва каквото има и отбелязва това като открит въпрос, вместо да остави човека без текст.

Всеки run пише хронологичен лог в posts/<име>/run.log (виж src/logging_utils.py).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.errors import AgentError, BriefingMissingError, BriefingNotFilledError
from src.llm.client import LLMClient
from src.logging_utils import setup_run_logger
from src.pipeline import consolidator, critic, formatting, media_picker, planner, writer
from src.pipeline.consolidator import PipelineResult
from src.pipeline.util import read_text_or_none, write_text

EXAMPLE_BRIEFING_PATH = Path("posts/_example/briefing.md")

SECOND_POST_FILENAME = "post_ii.md"
FINAL_CRITIQUE_FILENAME = "critique_final.md"


def _is_unfilled_template(briefing_text: str, template_path: Path = EXAMPLE_BRIEFING_PATH) -> bool:
    """
    Точно сравнение срещу posts/_example/briefing.md — хваща най-честия случай (`new` без
    последващо редактиране) безплатно, преди Планер/Писател/Критик да похарчат реални
    LLM извиквания върху бриф без съдържание. Не хваща частично попълнени бележки — това
    си остава работа на Критика.
    """
    template_text = read_text_or_none(template_path)
    if template_text is None:
        return False
    return briefing_text.strip() == template_text.strip()


def run_pipeline(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool = False,
    knowledge: BrandKnowledge | None = None,
) -> PipelineResult:
    """
    Оркестрира всичките седем стъпки за една папка на пост (виж схемата в докстринга на
    модула). Винаги връща PipelineResult с готово предложение — единствените изключения са
    за вход (липсващ/нередактиран briefing.md) и инфраструктура (недостъпен модел),
    хвърлени преди първото LLM извикване.
    """
    log = setup_run_logger(post_dir)
    log.info("=== Ново пускане на конвейера за '%s' (force=%s) ===", post_dir, force)

    briefing_path = post_dir / "briefing.md"
    briefing_text = read_text_or_none(briefing_path)
    if not briefing_text or not briefing_text.strip():
        log.error("Спряно: briefing.md липсва или е празен.")
        raise BriefingMissingError(str(post_dir))
    if _is_unfilled_template(briefing_text):
        log.error("Спряно: briefing.md е нередактираният шаблон от posts/_example/.")
        raise BriefingNotFilledError(str(post_dir))

    if knowledge is None:
        knowledge = load_brand_knowledge()

    warnings: list[str] = []
    open_notes: list[str] = []

    # [1] Планер
    log.info("[1] Планер...")
    plan = planner.run(post_dir, client, knowledge.stable_context, briefing_text, force=force)
    log.info("[1] тип: %s · формат: %s · тон: %s", plan.post_type, plan.format, plan.tone)
    log.info("[1] чувствителен: %s", "да" if plan.is_sensitive else "не")
    if plan.notes:
        log.info("[1] бележка на Планера: %s", plan.notes)

    # [2] Писател и [3] Media Picker вървят паралелно — независими една от друга
    log.info("[2]/[3] Писател + Media Picker (паралелно)...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        draft_future = executor.submit(
            writer.write_first_draft, post_dir, client, knowledge, plan, briefing_text, force=force
        )
        media_future = executor.submit(media_picker.run, post_dir, client, plan, briefing_text, force=force)
        post_i_text = draft_future.result()
        media_selection, no_photos = media_future.result()
    if no_photos:
        log.warning("Папката photos/ е празна — постът ще съдържа само текст.")
    log.info("[2]/[3] Post I: %d думи · снимки: %d избрани", len(post_i_text.split()), len(media_selection.selected))
    for item in media_selection.selected:
        log.info("[2]/[3] снимка: %s — %s", item.filename, item.reason)

    # [4] Критик върху Post I — намира какво да запълни вторият пост
    log.info("[4] Критик върху Post I...")
    critique = critic.run(post_dir, client, knowledge, plan, briefing_text, post_i_text, force=force)
    findings = critic.collect_findings(critique)
    log.info("[4] присъда: %s · %d находки за запълване", critique.verdict.value, len(findings))
    for finding in findings:
        log.info("[4] находка: %s", finding)

    # [5] Писател → Post II. Пропуска се само когато няма нито една находка — тогава няма
    # какво да запълва и втори модел ход би бил похарчен без причина.
    post_ii_text: str | None = None
    if findings:
        log.info("[5] Писател → Post II (запълва %d находки)...", len(findings))
        try:
            post_ii_text = writer.write_second_post(
                post_dir,
                client,
                knowledge,
                plan,
                briefing_text,
                post_i_text,
                findings,
                output_filename=SECOND_POST_FILENAME,
                force=force,
            )
            log.info("[5] Post II: %d думи", len(post_ii_text.split()))
        except AgentError as exc:
            # Вторият пост е подобрение, не предпоставка — без него консолидацията пада до
            # Post I, вместо целият run да остане без текст.
            open_notes.append(f"Вторият пост не беше написан ({exc.human_message}) — предложението е само от Post I.")
            log.error("[5] Post II се счупи: %s", exc.human_message)
    else:
        log.info("[5] Пропуснат — Критикът не намери какво да се запълва.")

    # [6] Консолидатор — официалното предложение от двата поста
    log.info("[6] Консолидатор (%s)...", "Post I + Post II" if post_ii_text else "само Post I")
    try:
        official_text, merged_from = consolidator.synthesize(
            post_dir,
            client,
            first_post=post_i_text,
            second_post=post_ii_text,
            findings=findings,
            force=force,
        )
    except AgentError as exc:
        # Последна линия: по-добре по-слабият от двата поста като предложение, отколкото
        # нищо. Кой точно е използван се вижда в merged_from и в open_notes.
        official_text = (post_ii_text or post_i_text).strip()
        merged_from = [SECOND_POST_FILENAME if post_ii_text else "post_i.md"]
        write_text(post_dir / "output" / "post.md", official_text + "\n")
        open_notes.append(
            f"Консолидаторът не успя да обедини двата поста ({exc.human_message}) — "
            f"предложението е {merged_from[0]} без синтез."
        )
        log.error("[6] Консолидаторът се счупи: %s", exc.human_message)
    log.info("[6] предложение: %d думи · от %s", len(official_text.split()), " + ".join(merged_from))

    # Сравнението срещу двете чернови става СЕГА, върху необработения синтез — след
    # автоматичните поправки по-долу текстът вече не е дословно равен на нищо и проверката
    # би станала безсмислена.
    if post_ii_text:
        bypassed = consolidator.synthesis_bypassed(official_text, post_i_text, post_ii_text)
        if bypassed:
            note = (
                f"Консолидаторът върна {bypassed} дословно — синтез между двата поста не се е "
                f"случил. Сравнете post_i.md и post_ii.md ръчно; вероятно има какво да се вземе "
                f"от другата версия."
            )
            open_notes.append(note)
            log.warning("[6] %s", note)

    # [6б] Механичните правила от корпуса — налагат се в кода, не се оставят на модел.
    # Върви ПРЕДИ финалната проверка, за да не хаби Критикът бележки за вече поправени неща,
    # и ПРЕДИ записа, за да е output/post.md винаги публикуем.
    formatted = formatting.enforce(official_text, knowledge.format_rules, brief_text=briefing_text)
    applied_fixes = formatted.fixes
    if formatted.text != official_text:
        official_text = formatted.text
        write_text(post_dir / "output" / "post.md", official_text + "\n")
    for fix in applied_fixes:
        log.info("[6б] Поправено автоматично: %s", fix)
    for unenforceable in formatted.unenforceable:
        log.warning("[6б] Правило от корпуса, което кодът не може да наложи: %s", unenforceable)
    for note in formatted.notes:
        log.info("[6б] бележка: %s", note)
    open_notes.extend(formatted.notes)

    # [7] Критик върху предложението — само констатира, не спира. Целта е човекът да види
    # какво остава открито в текста, който реално ще публикува, а не в междинна чернова.
    log.info("[7] Критик върху предложението (информативно)...")
    try:
        final_critique = critic.run(
            post_dir,
            client,
            knowledge,
            plan,
            briefing_text,
            official_text,
            filename=FINAL_CRITIQUE_FILENAME,
            force=force,
        )
        final_notes = critic.review_notes(final_critique)
        open_notes.extend(final_notes)
        log.info("[7] %d открити въпроса в предложението", len(final_notes))
        for note in final_notes:
            log.info("[7] открит въпрос: %s", note)
    except AgentError as exc:
        open_notes.append(f"Финалната проверка не успя ({exc.human_message}) — прегледайте текста ръчно.")
        log.error("[7] Финалната проверка се счупи: %s", exc.human_message)

    if any(item.possible_minor for item in media_selection.selected):
        warning_text = (
            "Избрана снимка е маркирана от Media Picker като вероятно съдържаща непълнолетни лица — "
            "прегледайте ръчно преди публикуване."
        )
        warnings.append(warning_text)
        log.warning(warning_text)

    status = "ready_with_notes" if (open_notes or warnings) else "ready"
    log.info("Финален статус: %s (%d открити въпроса, %d предупреждения)", status, len(open_notes), len(warnings))
    for note in open_notes:
        log.warning("Открит въпрос: %s", note)

    return consolidator.finalize(
        post_dir,
        client,
        plan,
        media_selection,
        no_photos=no_photos,
        status=status,
        open_notes=open_notes,
        warnings=warnings,
        merged_from=merged_from,
        applied_fixes=applied_fixes,
    )
