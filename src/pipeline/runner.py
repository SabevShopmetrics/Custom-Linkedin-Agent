"""
Оркестрира конвейера и управлява възобновяемостта.

Верига v3 — подразбиране, от 2026-08-11, потребителска инструкция — виж
docs/technical/process-3.md):

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

Верига v5 — условна, зад `PIPELINE_CHAIN=v5` в .env (подразбиране остава v3). Надгражда
docs/technical/process-4.md (никога построена в код) по
docs/research-plans/research-plan-15-pipeline-speed-quality-rearchitecture.md:

  [1]   Планер                                              (непроменено)
  [1.5] Факт-регистър → facts_pool.json                      код, не LLM
  [2]/[3] Писател × N кандидата + Media Picker (паралелно)   независими кандидати
  [4]   Филтър за проследимост → facts_check.json            код, не LLM
  [5]   Съдия (pairwise) → judgement.json                    избира, НЕ пише
  [6]   Механичните правила (formatting.py)                  (непроменено)
  [7]   Финален критик — АСИНХРОННО, не блокира резултата

`_run_pipeline_v3` е точно старата функция, непипната отвъд извличането в отделна функция
и приемането на `log` като параметър (изборът на верига е конфигурация в `run_pipeline`,
не преправяне на всяка стъпка — process-4.md, Част 0). `_run_pipeline_v5` е изцяло нов код.

Всеки run пише хронологичен лог в posts/<име>/run.log (виж src/logging_utils.py).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.errors import AgentError, BriefingMissingError, BriefingNotFilledError
from src.llm.client import LLMClient
from src.logging_utils import setup_run_logger
from src.pipeline import consolidator, critic, facts_pool, formatting, judge, media_picker, planner, verifier, writer
from src.pipeline.consolidator import PipelineResult
from src.pipeline.util import read_json, read_text_or_none, write_json, write_text

EXAMPLE_BRIEFING_PATH = Path("posts/_example/briefing.md")

SECOND_POST_FILENAME = "post_ii.md"
FINAL_CRITIQUE_FILENAME = "critique_final.md"


def _resolution_summary(initial_count: int, final_count: int) -> str:
    """
    Едно изречение сравняващо находките на Критика на Post I срещу тези на финалния текст —
    виж research-plan-13-critic-resolution-visibility.md, Част 2. Не съпоставя КОИ точно
    находки съвпадат (Post II преформулира, не копира — точно съвпадение на текста е
    ненадеждно), само броя, което е достатъчно за да различи "адресирани" от "непроменени".
    """
    if final_count == 0:
        label = "напълно адресирани"
    elif final_count < initial_count:
        label = "частично адресирани"
    else:
        label = "непроменени/влошени"
    return (
        f"Критикът намери {initial_count} находки в Post I; {final_count} остават отворени "
        f"след Post II + Консолидатор ({label})."
    )


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
    Валидира входа (общо за двете вериги), после разклонява на v3 (подразбиране) или v5
    според `client.config.pipeline_chain` — изборът е конфигурация, не код (process-4.md,
    Част 0). Тестови дубли без `.config` (виж tests/) падат обратно на v3, непроменено.
    """
    log = setup_run_logger(post_dir)

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

    config = getattr(client, "config", None)
    chain = (getattr(config, "pipeline_chain", None) or "v3").strip().lower()
    if chain not in ("v3", "v5"):
        chain = "v3"  # непозната стойност в .env → безопасно подразбиране, не изключение

    log.info("=== Ново пускане на конвейера за '%s' (force=%s, chain=%s) ===", post_dir, force, chain)

    if chain == "v5":
        return _run_pipeline_v5(post_dir, client, force=force, knowledge=knowledge, briefing_text=briefing_text, log=log)
    return _run_pipeline_v3(post_dir, client, force=force, knowledge=knowledge, briefing_text=briefing_text, log=log)


# ============================================================================ v3 (подразбиране)


def _run_pipeline_v3(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool,
    knowledge: BrandKnowledge,
    briefing_text: str,
    log,
) -> PipelineResult:
    warnings: list[str] = []
    open_notes: list[str] = []

    # Стъпки 1-4 НЕ са подсигурени като 5/6/7 (виж докстринга на модула) — грешка тук спира
    # целия run. Ако е AgentError (напр. ModelUnavailableError), детайлът (суровата причина
    # — HTTP статус, изключение при декодиране на снимка и т.н.) иначе се губи безследно:
    # cli.py показва само exc.human_message, а run.log дотук няма нито ред за самата грешка.
    # Ето защо тук изрично се логва human_message + detail, преди изключението да продължи
    # нагоре — намерено при стрес тест с развалена снимка (2026-08-17): конвейерът спираше с
    # общото "моделът не отговори... проверете API ключа", без следа, че истинската причина
    # беше нечетимо съдържание на снимка, не недостъпен модел.
    try:
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
        log.info(
            "[2]/[3] Post I: %d думи · снимки: %d избрани", len(post_i_text.split()), len(media_selection.selected)
        )
        for item in media_selection.selected:
            log.info("[2]/[3] снимка: %s — %s", item.filename, item.reason)

        # [4] Критик върху Post I — намира какво да запълни вторият пост
        log.info("[4] Критик върху Post I...")
        critique = critic.run(post_dir, client, knowledge, plan, briefing_text, post_i_text, force=force)
        findings = critic.collect_findings(critique)

        # Дължината под минимума на корпуса (knowledge/voice/tone.md, изведена от 12
        # реални поста на самата страница) досега стигаше до човек само като бележка на
        # formatting.enforce() СЛЕД Консолидатора — твърде късно, за да я поправи Post II.
        # Потребителска инструкция 2026-08-17: минимумът трябва реално да се приложи, не
        # само да се отбележи. Код-ниво проверка тук (детерминирана, не LLM преценка),
        # инжектирана директно в findings — същия модел като GDPR проверките на критика.
        words_min = knowledge.format_rules.words_min
        post_i_words = len(post_i_text.split())
        if words_min is not None and post_i_words < words_min:
            findings.append(
                f"Дължина: постът е {post_i_words} думи, под минимума {words_min} от корпуса "
                f"(knowledge/voice/tone.md) — добави още конкретни факти (число, детайл, "
                f"резултат), не общи изречения, за да стигнеш поне {words_min} думи."
            )
            log.info("[4] находка (код): под минимума %d думи (Post I: %d)", words_min, post_i_words)

        log.info("[4] присъда: %s · %d находки за запълване", critique.verdict.value, len(findings))
        for finding in findings:
            log.info("[4] находка: %s", finding)
    except AgentError as exc:
        log.error("Спряно: %s", exc.human_message)
        if exc.detail:
            log.error("Причина (техническа подробност): %s", exc.detail)
        raise

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
    resolution_summary: str | None = None
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

        # Критикът вижда текста два пъти (Post I тук горе, крайния текст точно сега), но
        # дотук никой не сравняваше двете числа — виж
        # docs/research-plans/research-plan-13-critic-resolution-visibility.md. Чисто
        # аритметика върху вече изчислени стойности, без нов LLM разход.
        if findings:
            resolution_summary = _resolution_summary(len(findings), len(final_notes))
            log.info("[7] %s", resolution_summary)
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
        resolution_summary=resolution_summary,
    )


# ============================================================================ v5 (PIPELINE_CHAIN=v5)


def _run_pipeline_v5(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool,
    knowledge: BrandKnowledge,
    briefing_text: str,
    log,
) -> PipelineResult:
    """
    Виж докстринга на модула за схемата. Разлика от v3, накратко: няма Post II/Консолидатор
    цикъл, който пренаписва — има N независими кандидата, код-ниво филтър за проследимост,
    и Съдия, който избира. Стъпка [7] тук е информативна, както в v3, но тече асинхронно.
    """
    warnings: list[str] = []
    open_notes: list[str] = []
    config = client.config  # само v5 стигна дотук, значи client.config съществува (виж run_pipeline)
    n = max(1, config.writer_candidates)

    try:
        # [1] Планер — идентично на v3.
        log.info("[1] Планер...")
        plan = planner.run(post_dir, client, knowledge.stable_context, briefing_text, force=force)
        log.info("[1] тип: %s · формат: %s · тон: %s", plan.post_type, plan.format, plan.tone)
        log.info("[1] чувствителен: %s", "да" if plan.is_sensitive else "не")
        if plan.notes:
            log.info("[1] бележка на Планера: %s", plan.notes)

        # [1.5] Факт-регистър — код, не LLM (виж src/pipeline/facts_pool.py).
        log.info("[1.5] Факт-регистър...")
        pool = facts_pool.build(post_dir, briefing_text, knowledge, force=force)
        log.info(
            "[1.5] %d твърдения от брифа · %d член(ове) с потвърдено съгласие",
            len(pool.briefing_claims),
            len(pool.mentionable_members),
        )

        # [2]/[3] Писател × N кандидата + Media Picker — паралелно.
        log.info("[2]/[3] Писател × %d кандидата + Media Picker (паралелно)...", n)
        with ThreadPoolExecutor(max_workers=2) as executor:
            candidates_future = executor.submit(
                writer.write_candidates, post_dir, client, knowledge, plan, briefing_text, pool, n=n, force=force
            )
            media_future = executor.submit(media_picker.run, post_dir, client, plan, briefing_text, force=force)
            candidates = candidates_future.result()
            media_selection, no_photos = media_future.result()
        if no_photos:
            log.warning("Папката photos/ е празна — постът ще съдържа само текст.")
        for filename, text in candidates:
            log.info("[2]/[3] %s: %d думи", filename, len(text.split()))
        for item in media_selection.selected:
            log.info("[2]/[3] снимка: %s — %s", item.filename, item.reason)

        # [4] Филтър за проследимост — код, не LLM (виж src/pipeline/verifier.py).
        log.info("[4] Филтър за проследимост (код, без LLM извикване)...")
        verifications = verifier.check_all(post_dir, candidates, briefing_text, pool, knowledge, force=force)
        for v in verifications:
            blocked_note = " · БЛОКИРАН (rules/forbidden.md)" if v.blocked else ""
            log.info("[4] %s: %d непроследими твърдения%s", v.filename, v.unverified_count, blocked_note)

        # [5] Съдия — сравнява по двойки, избира, НЕ пише (виж src/pipeline/judge.py).
        log.info("[5] Съдия (сравнение по двойки)...")
        judgement = judge.select(post_dir, client, knowledge, plan, candidates, verifications, force=force)
        log.info("[5] избран: %s · %s", judgement.chosen_filename, judgement.reasoning_summary or "(без обобщение)")
        if judgement.fallback_used:
            open_notes.append(judgement.reasoning_summary)
            log.warning("[5] %s", judgement.reasoning_summary)

        chosen_verification = next(v for v in verifications if v.filename == judgement.chosen_filename)
        for claim in chosen_verification.claims:
            if not claim.verified:
                open_notes.append(
                    f"Недоказано твърдение — или го проследи до брифа/facts.yaml/members.yaml, "
                    f"или го премахни: \"{claim.claim}\""
                )
        for violation in chosen_verification.rule_violations:
            label = "GDPR" if violation.rule == "gdpr" else "rules/forbidden.md"
            open_notes.append(f"[{label}] {violation.detail}")
    except AgentError as exc:
        log.error("Спряно: %s", exc.human_message)
        if exc.detail:
            log.error("Причина (техническа подробност): %s", exc.detail)
        raise

    official_text = judgement.chosen_text.strip()
    merged_from = [judgement.chosen_filename]
    write_text(post_dir / "output" / "post.md", official_text + "\n")

    # [6] Механичните правила от корпуса — идентично на v3's [6б].
    formatted = formatting.enforce(official_text, knowledge.format_rules, brief_text=briefing_text)
    applied_fixes = formatted.fixes
    if formatted.text != official_text:
        official_text = formatted.text
        write_text(post_dir / "output" / "post.md", official_text + "\n")
    for fix in applied_fixes:
        log.info("[6] Поправено автоматично: %s", fix)
    for unenforceable in formatted.unenforceable:
        log.warning("[6] Правило от корпуса, което кодът не може да наложи: %s", unenforceable)
    for note in formatted.notes:
        log.info("[6] бележка: %s", note)
    open_notes.extend(formatted.notes)

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

    # [8] Бележка за EU AI Act, чл. 50 — чисто добавъчна метаданна (план 15, Част 2.9).
    # НЕ твърди категорично дали маркировка е нужна — това зависи от бъдеща политика за
    # автономно публикуване без преглед, извън обхвата на runner.py. Подготовка, не присъда.
    ai_act_meta = {
        "ai_act": {
            "note": (
                "Изключението за 'съществен човешки преглед' по чл. 50, ал. 4 вероятно важи, "
                "докато всеки пост минава през човек преди публикуване (виж "
                "research-plan-15-pipeline-speed-quality-rearchitecture.md, Част 2.9). "
                "Полето е подготовка за деня, в който автономно публикуване без преглед "
                "стане реалност — тогава машинно-четима маркировка вероятно става задължителна."
            ),
        }
    }

    result = consolidator.finalize(
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
        resolution_summary=None,
        extra_meta=ai_act_meta,
    )

    # [7] Финален критик — АСИНХРОННО (план 15, Част 4/1.1: тази стъпка сама изяжда ~70s в
    # v3, без да променя изхода — чисто информативна и там). Виж _start_async_final_check
    # защо не пише през споделения logger.
    _start_async_final_check(post_dir, client, knowledge, plan, briefing_text, official_text)

    return result


def _start_async_final_check(post_dir, client, knowledge, plan, briefing_text, official_text) -> None:
    """
    Стъпка [7] на v5 — тече във фонов daemon thread, за да не блокира вече показания на
    човека резултат.

    НЕ използва logging.getLogger(LOGGER_NAME) (src/logging_utils.py): setup_run_logger()
    чисти handler-ите на ВСЕКИ извикване, а ако междувременно потребителят пусне НОВ пост
    (втори run_pipeline в същия процес — реален случай в TUI/REPL), споделеният logger вече
    сочи към ДРУГ run.log файл. Находките на този фонов thread биха се записали в грешната
    папка. Затова пише директно във файла на СВОЯ post_dir, обвързан по стойност още преди
    thread-ът да тръгне — никога през глобално споделено състояние.

    `daemon=True`: ако процесът приключи (CLI `generate`, скрипт) преди фоновата проверка
    да свърши, тя просто не се довършва — по-добре това, отколкото процес, който виси
    заради стъпка, чийто резултат вече е показан.
    """

    def _worker() -> None:
        log_path = post_dir / "run.log"
        try:
            _append_run_log(log_path, "[7] Финален критик (async, информативно)...")
            final_critique = critic.run(
                post_dir,
                client,
                knowledge,
                plan,
                briefing_text,
                official_text,
                filename=FINAL_CRITIQUE_FILENAME,
                force=False,
            )
            final_notes = critic.review_notes(final_critique)
            _append_run_log(log_path, f"[7] {len(final_notes)} открити въпроса в предложението (async)")
            for note in final_notes:
                _append_run_log(log_path, f"[7] открит въпрос: {note}")
            _merge_async_notes_into_meta(post_dir, final_notes)
        except AgentError as exc:
            _append_run_log(log_path, f"[7] Финалната проверка (async) се счупи: {exc.human_message}")

    thread = threading.Thread(target=_worker, daemon=True, name=f"final-check-{post_dir.name}")
    thread.start()


def _append_run_log(log_path: Path, message: str) -> None:
    """Пряк запис във файл, без logging модула — виж докстринга на _start_async_final_check."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [INFO] {message}\n")


def _merge_async_notes_into_meta(post_dir: Path, final_notes: list[str]) -> None:
    """
    Дописва находките от асинхронната проверка в output/meta.json СЛЕД като той вече е
    записан от consolidator.finalize() по-горе — read-merge-write, за да не се загуби
    останалата одитна следа. bench.py и всеки друг читател на meta.json вижда пълната
    картина, щом фоновата проверка приключи. TUI/REPL, които вече са показали резултата на
    екрана, НЕ се обновяват визуално — известно, документирано ограничение (план 15,
    Недоказани твърдения), не пропуск.
    """
    meta_path = post_dir / "output" / "meta.json"
    if not meta_path.exists() or not final_notes:
        return
    try:
        meta = read_json(meta_path)
    except (OSError, ValueError):
        return
    meta["open_notes"] = list(meta.get("open_notes", [])) + list(final_notes)
    if meta.get("status") == "ready":
        meta["status"] = "ready_with_notes"
    write_json(meta_path, meta)
