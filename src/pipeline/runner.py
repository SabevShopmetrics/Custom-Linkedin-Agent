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
  [7] Критик върху предложението → critique_final.md — само констатира какво остава открито,
      АСИНХРОННО от 2026-08-25 (виж _start_async_final_check) — не блокира резултата,
      реални пускания показаха 70-350+ секунди чисто чакане за информативна стъпка

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
Стъпки 5 и 6 са допълнително подсигурени — ако някоя от тях се счупи, конвейерът записва
каквото има и отбелязва това като открит въпрос, вместо да остави човека без текст. Стъпка 7
е асинхронна (виж по-горе) — грешка там се логва в run.log на фоновия thread, не стига до
резултата, който човекът вече е видял.

Верига v5 — условна, зад `PIPELINE_CHAIN=v5` в .env (подразбиране остава v3). Надгражда
docs/technical/process-4.md (никога построена в код) по
docs/research-plans/research-plan-15-pipeline-speed-quality-rearchitecture.md:

  [1]   Планер                                              (непроменено)
  [1.5] Факт-регистър → facts_pool.json                      код, не LLM
  [2]/[3] Писател × N кандидата + Media Picker (паралелно)   независими кандидати
  [4]   Филтър за проследимост → facts_check.json            код (факти/GDPR) + лека
                                                               паралелна LLM проверка само
                                                               за rules/forbidden.md на
                                                               кандидат (2026-08-25 — виж
                                                               verifier.py защо не е "без LLM")
  [5]   Съдия (pairwise) → judgement.json                    избира, НЕ пише
  [6]   Механичните правила (formatting.py)                  (непроменено)
  [7]   Финален критик — АСИНХРОННО, не блокира резултата

`_run_pipeline_v3` е точно старата функция, непипната отвъд извличането в отделна функция
и приемането на `log` като параметър (изборът на верига е конфигурация в `run_pipeline`,
не преправяне на всяка стъпка — process-4.md, Част 0). `_run_pipeline_v5` е изцяло нов код.

Всеки run пише хронологичен лог в posts/<име>/run.log (виж src/logging_utils.py).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.errors import AgentError, BriefingMissingError, BriefingNotFilledError
from src.llm.client import LLMClient
from src.logging_utils import setup_run_logger
from src.pipeline import consolidator, critic, facts_pool, formatting, judge, media_picker, modes, planner, verifier, writer
from src.pipeline.consolidator import PipelineResult
from src.pipeline.util import read_json, read_text_or_none, write_json, write_text

EXAMPLE_BRIEFING_PATH = Path("posts/_example/briefing.md")

SECOND_POST_FILENAME = "post_ii.md"
FINAL_CRITIQUE_FILENAME = "critique_final.md"


@dataclass
class FinalCheckUpdate:
    """
    Резултатът от стъпка [7], предаден на `on_final_check` в МОМЕНТА, в който фоновият
    daemon thread приключи — за разлика от `_merge_async_notes_into_meta` (записва в
    output/meta.json на диска), това е нотификация НА ЖИВО за викащия (TUI/REPL), който
    вече е показал резултата на екрана и може междувременно да е преминал към друг пост.

    Виж research-plan-29 и докстринга на `run_pipeline::wait_for_final_check_seconds` за
    защо стъпка [7] е async — TUI/REPL не могат да си позволят 70-350+s блокиране на всяко
    пускане. Проблемът, който `on_final_check` решава: без него намереното от стъпка [7]
    стигаше само до файла на диска, а човекът, вече гледащ "✓ ready" на екрана, нямаше как
    да разбере, че междувременно се е появила бележка — освен ако сам провери meta.json.
    """

    post_dir: Path
    status: str  # "ok" | "failed" — огледално на meta.json's final_check.status
    notes: list[str] = field(default_factory=list)
    resolution_summary: str | None = None
    error: str | None = None


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


def _effective_format_rules(knowledge: BrandKnowledge, mode: modes.PostMode) -> "formatting.FormatRules":
    """
    FormatRules на корпуса, с думите-диапазон заменени от избрания режим за дължина
    (pipeline/modes.py), ако той има мнение (words_min не е None). "Фокус" няма мнение —
    връща знанието непроменено, тоест поведението без изричен избор на режим е точно
    същото, каквото беше преди модовете да съществуват.
    """
    if mode.words_min is None and mode.words_max is None:
        return knowledge.format_rules
    return dataclasses.replace(knowledge.format_rules, words_min=mode.words_min, words_max=mode.words_max)


def _ai_act_meta() -> dict:
    """
    Бележка за EU AI Act, чл. 50 — чисто добавъчна метаданна (план 15, Част 2.9). НЕ твърди
    категорично дали маркировка е нужна — това зависи от бъдеща политика за автономно
    публикуване без преглед, извън обхвата на runner.py. Подготовка, не присъда.

    До 2026-08-25 това се пишеше само в _run_pipeline_v5 — v3 (verigата, по подразбиране)
    нямаше НИКАКВА следа от тази проверка в meta.json. Изнесено тук като споделена функция,
    за да получат двете вериги еднакво покритие, без да се дублира текстът на бележката.
    """
    return {
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
    mode: str | None = None,
    today: date | None = None,
    wait_for_final_check_seconds: float = 0.0,
    on_final_check: Callable[[FinalCheckUpdate], None] | None = None,
) -> PipelineResult:
    """
    Валидира входа (общо за двете вериги), после разклонява на v3 (подразбиране) или v5
    според `client.config.pipeline_chain` — изборът е конфигурация, не код (process-4.md,
    Част 0). Тестови дубли без `.config` (виж tests/) падат обратно на v3, непроменено.

    `mode` е режимът за дължина (pipeline/modes.py — "kadar"/"fokus"/"panorama"), избран
    ИЗРИЧНО от човека (REPL `/mode`, TUI `/mode`, CLI `--mode`), никога извеждан от модел.
    None/непознат/подразбиращ се "fokus" не пипа нищо — точно старото поведение.

    `today` е инжектирана дата (същия модел като repl.py::unique_post_dir) — само за
    тестваемост на formatting.py::_flag_anniversary_mismatch; None означава истинската
    днешна дата.

    `wait_for_final_check_seconds` (виж research-plan-29): 0 (подразбиране) запазва старото
    fire-and-forget поведение на стъпка [7] — правилно за REPL/chat, където блокирането би
    забавило всяка следваща команда с до 70-350+s (потребителска инструкция 2026-08-25,
    "~600 секунди, оправи го", е точно причината стъпка [7] да стане async). CLI `generate`
    е ЕДНОКРАТНО извикване — процесът приключва веднага след печат на резюмето и убива
    daemon нишката на стъпка [7], преди тя изобщо да напише резултат в meta.json (затова
    `final_check.status` оставаше "pending" завинаги, не само "невидяно на екрана", какъвто
    беше познатият, документиран компромис). При стойност > 0, `generate` изчаква толкова
    секунди стъпка [7] да завърши, преди да върне резултата — само best-effort: ако не успее
    в срока, продължава без нея (текущото поведение), не увисва безкрайно.

    `on_final_check` (виж research-plan-34 и докстринга на `FinalCheckUpdate`): извиква се
    от фоновия daemon thread на стъпка [7], В МОМЕНТА, в който тя приключи — независимо
    колко след като `run_pipeline` вече е върнала резултат. TUI/REPL го подават, за да
    покажат закъснялата бележка на екрана вместо тя да остане само в meta.json, невидяна
    без ръчна проверка (документираното ограничение по-горе). CLI `generate` не го подава —
    там `wait_for_final_check_seconds` вече решава същия проблем синхронно.
    """
    log = setup_run_logger(post_dir)
    mode_obj = modes.get_mode(mode)

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

    log.info(
        "=== Ново пускане на конвейера за '%s' (force=%s, chain=%s, режим=%s) ===",
        post_dir, force, chain, mode_obj.label,
    )

    if chain == "v5":
        return _run_pipeline_v5(
            post_dir,
            client,
            force=force,
            knowledge=knowledge,
            briefing_text=briefing_text,
            log=log,
            mode=mode_obj,
            today=today,
            wait_for_final_check_seconds=wait_for_final_check_seconds,
            on_final_check=on_final_check,
        )
    return _run_pipeline_v3(
        post_dir,
        client,
        force=force,
        knowledge=knowledge,
        briefing_text=briefing_text,
        log=log,
        mode=mode_obj,
        today=today,
        wait_for_final_check_seconds=wait_for_final_check_seconds,
        on_final_check=on_final_check,
    )


# ============================================================================ v3 (подразбиране)


def _run_pipeline_v3(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool,
    knowledge: BrandKnowledge,
    briefing_text: str,
    log,
    mode: modes.PostMode = modes.get_mode(None),
    today: date | None = None,
    wait_for_final_check_seconds: float = 0.0,
    on_final_check: Callable[[FinalCheckUpdate], None] | None = None,
) -> PipelineResult:
    warnings: list[str] = []
    open_notes: list[str] = []
    effective_rules = _effective_format_rules(knowledge, mode)
    founded_year = (knowledge.facts or {}).get("organization", {}).get("founded")

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
        if plan.requires_topic_replacement:
            log.info("[1] темата на брифа нарушава ЗАБРАНЕНО — нужен е нов повод от човек.")

        # Критик/Post II се пропускат при insufficient_brief (виж research-plan-18/19) И при
        # requires_topic_replacement (виж research-plan-26-forbidden-topics-and-numeric-
        # relationships.md, Част А) — реален случай, открит при живо тестване: без този байпас
        # Критикът третираше честния отказ на Писателя за забранена тема (напр. религиозен
        # повод) като "проблем с формата" и настояваше за "нова версия със заменена
        # инициатива" — Post II изпълни искането, като си измисли ЦЯЛО фиктивно събитие
        # (конкретна дата, конкретен дневен ред), само за да прилича на нормален пост.
        skip_critic_and_second_post = plan.insufficient_brief or plan.requires_topic_replacement

        # [2] Писател и [3] Media Picker вървят паралелно — независими една от друга
        log.info("[2]/[3] Писател + Media Picker (паралелно)...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            draft_future = executor.submit(
                writer.write_first_draft,
                post_dir,
                client,
                knowledge,
                plan,
                briefing_text,
                force=force,
                mode_instruction=mode.instruction,
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

        # [4] Критик върху Post I — намира какво да запълни вторият пост. Пропуска се
        # изцяло, когато Планерът е маркирал брифа като лишен от реално съдържание (виж
        # research-plan-18-critique-of-plan-17.md, А2/B4): живото тестване от План 17
        # показа, че иначе Критикът третира честния отказ на Писателя ("Брифът за
        # събитието не е предоставен") като стилов проблем и принуждава Post II да го
        # запълни с изцяло измислен текст. Без Критик няма находки → стъпка [5] отдолу се
        # пропуска автоматично (findings == []) → стъпка [6] минава по съществуващия
        # "само механично чистене" път (second_post=None), който изрично не добавя ново
        # съдържание — честният отказ на Писателя остава непроменен.
        findings: list[str] = []
        if skip_critic_and_second_post:
            log.info(
                "[4]-[6] Пропуснати — брифът е маркиран от Планера като %s.",
                "недостатъчен" if plan.insufficient_brief else "нарушаващ ЗАБРАНЕНО (нужен нов повод)",
            )
        else:
            log.info("[4] Критик върху Post I...")
            critique = critic.run(post_dir, client, knowledge, plan, briefing_text, post_i_text, force=force)
            findings = critic.collect_findings(critique)

            # Дължината под минимума на корпуса (knowledge/voice/tone.md, изведена от 12
            # реални поста на самата страница) досега стигаше до човек само като бележка на
            # formatting.enforce() СЛЕД Консолидатора — твърде късно, за да я поправи Post II.
            # Потребителска инструкция 2026-08-17: минимумът трябва реално да се приложи, не
            # само да се отбележи. Код-ниво проверка тук (детерминирана, не LLM преценка),
            # инжектирана директно в findings — същия модел като GDPR проверките на критика.
            words_min = effective_rules.words_min
            post_i_words = len(post_i_text.split())
            if words_min is not None and post_i_words < words_min:
                # Ако Планерът вече е класифицирал брифа като истински "тънък" (не просто
                # "тънък, но достатъчен" — виж planner.notes_flag_thin_brief) в стъпка [1],
                # тази находка НЕ бива безусловно да иска "добави факти" — реален случай (живо
                # тестване, 2026-08-31, виж research-plan-25-adversarial-live-testing-
                # findings.md, Б-3): точно тази безусловна формулировка накара Post II да
                # запълни дупката с генерична философска проза (същия клас нарушение, който
                # Критикът току-що отхвърли на Post I), защото инструкцията ѝ противоречеше
                # директно на "спри честно" правилото от tone.md, без изобщо да провери дали
                # брифът РЕАЛНО има още факти за добавяне.
                #
                # plan.is_sensitive проверява СЪЩОТО и отделно от текстовото "тънък" — реален,
                # по-сериозен случай (живо тестване, 2026-09-01, виж research-plan-27): бриф за
                # уволнение на конкретен, назован служител. Планерът написа бележка изцяло за
                # чувствителност/тон (HR/юридически контрол, "без детайли за поведението"), без
                # думата "тънък" изобщо — защото фокусът му беше сензитивността, не дължината.
                # planner.notes_flag_thin_brief() затова върна False и находката поиска "добави
                # факти", което накара Писателя да си измисли номер на заповед и позоваване на
                # Трудовия кодекс — нито едното в брифа — за реално, назовано лице. За
                # чувствителни теми "добави факти, които не съществуват" е несравнимо по-опасно
                # от генерична философска проза, затова тук печели безусловно, независимо дали
                # Планерът е използвал точно думата "тънък".
                if plan.is_sensitive or planner.notes_flag_thin_brief(plan.notes):
                    findings.append(
                        f"Дължина: постът е {post_i_words} думи, под минимума {words_min}. "
                        f"Планерът вече отбеляза тънък бриф — ако честните факти в брифа са "
                        f"изчерпани, ОСТАВИ поста така: по-кратък и честен е по-добър от "
                        f"удължен с измислена конкретика или преповтаряне на темата в bullet-и."
                    )
                else:
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
                mode_instruction=mode.instruction,
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
    formatted = formatting.enforce(
        official_text,
        effective_rules,
        brief_text=briefing_text,
        has_photos=bool(media_selection.selected),
        founded_year=founded_year,
        today=today,
    )
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

    if any(item.possible_minor for item in media_selection.selected):
        warning_text = (
            "Избрана снимка е маркирана от Media Picker като вероятно съдържаща непълнолетни лица — "
            "прегледайте ръчно преди публикуване."
        )
        warnings.append(warning_text)
        log.warning(warning_text)

    # research-plan-34-speed-quality-second-pass.md (Част 4): Media Picker досега подбираше
    # снимки без преценка дали пасват тематично на брифа — открито на живо (хакатон бриф,
    # избрани общи networking снимки без коментар). open_notes, не warnings — несъответствие
    # на снимка изисква преглед, не е GDPR-клас задължителна спирачка.
    for item in media_selection.selected:
        if item.topical_mismatch:
            reason_suffix = f": {item.mismatch_reason}" if item.mismatch_reason else ""
            open_notes.append(
                f"Media Picker флагна '{item.filename}' като вероятно несъответстваща тематично "
                f"на брифа{reason_suffix} — прегледайте дали снимката пасва."
            )

    if plan.insufficient_brief:
        open_notes.append(
            "Брифът изглежда без достатъчно съдържание за пост — Критик/Post II бяха "
            "пропуснати автоматично; текстът по-долу е буквалната чернова на Писателя."
        )
    elif plan.requires_topic_replacement:
        open_notes.append(
            "ТЕМАТА НА БРИФА НАРУШАВА ПРАВИЛАТА (rules/forbidden.md) — Критик/Post II бяха "
            "пропуснати автоматично, за да остане честният отказ на Писателя непроменен, "
            "вместо да бъде преправен да изглежда като нормален пост. Нужен е изцяло нов "
            "повод от човек, не само редакция на текста по-долу."
        )

    if plan.is_sensitive:
        open_notes.append(
            "ЧУВСТВИТЕЛНА ТЕМА — Планерът прецени, че тя изисква преглед и одобрение преди "
            "публикуване (CLAUDE.md: автономност хибрид — рутинните автономно, чувствителните "
            "с одобрение). Прегледайте лично, дори ако няма други бележки по-долу."
        )

    status = "ready_with_notes" if (open_notes or warnings) else "ready"
    log.info("Финален статус: %s (%d открити въпроса, %d предупреждения)", status, len(open_notes), len(warnings))
    for note in open_notes:
        log.warning("Открит въпрос: %s", note)

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
        # skip_critic_and_second_post, не само insufficient_brief — requires_topic_replacement
        # ползва СЪЩИЯ REPL "напиши детайли сега, за да генерирам наново" път (repl.py), защото
        # там нужният отговор е точно същият: нов, различен повод от човека.
        insufficient_brief=skip_critic_and_second_post,
        extra_meta={
            **_ai_act_meta(),
            "final_check": {"status": "skipped"} if skip_critic_and_second_post else {"status": "pending"},
        },
    )

    # [7] Критик върху предложението — АСИНХРОННО, огледално на v5's _start_async_final_check
    # (виж докстринга там). Потребителска инструкция 2026-08-25 ("~600 секунди, оправи го"):
    # реален run.log показа тази стъпка да отнема 70-350+ секунди — чисто информативна (не
    # пипа official_text/output/post.md, само докладва какво остава открито), но досега
    # блокираше отговора на човека точно толкова дълго. Бележките и resolution_summary
    # (план 13) се дописват в meta.json, щом фоновата проверка приключи. Известно ограничение,
    # същото като в v5: TUI/REPL, вече показали резултата, не се обновяват визуално.
    #
    # Пропусната изцяло за insufficient_brief (виж research-plan-19-critique-of-phase1-
    # implementation.md, А-2/А-3): (1) живият тест от План 17 вече показа нулева стойност
    # на тази проверка точно върху кратък, честен отказ на Писателя; (2) B4 (repl.py) може
    # автоматично да пусне ВТОРО генериране върху СЪЩАТА папка веднага след потребителски
    # отговор — ако тази async проверка на ПЪРВОТО (изоставено) пускане все още тече, докато
    # второто вече е записало нов meta.json, read-merge-write-ът ѝ презаписва бележки за
    # текст, който вече не е на екрана.
    if skip_critic_and_second_post:
        log.info("[7] Пропусната — брифът е маркиран от Планера като недостатъчен/със забранена тема (виж [4]-[6]).")
    else:
        log.info("[7] Критик върху предложението (async, информативно)...")
        thread = _start_async_final_check(
            post_dir, client, knowledge, plan, briefing_text, official_text, findings=findings, on_final_check=on_final_check
        )
        if wait_for_final_check_seconds > 0:
            result = _await_final_check(thread, post_dir, result, wait_for_final_check_seconds, log)

    return result


# ============================================================================ v5 (PIPELINE_CHAIN=v5)


def _run_pipeline_v5(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool,
    knowledge: BrandKnowledge,
    briefing_text: str,
    log,
    mode: modes.PostMode = modes.get_mode(None),
    today: date | None = None,
    wait_for_final_check_seconds: float = 0.0,
    on_final_check: Callable[[FinalCheckUpdate], None] | None = None,
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
    effective_rules = _effective_format_rules(knowledge, mode)
    founded_year = (knowledge.facts or {}).get("organization", {}).get("founded")

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
                writer.write_candidates,
                post_dir,
                client,
                knowledge,
                plan,
                briefing_text,
                pool,
                n=n,
                force=force,
                mode_instruction=mode.instruction,
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

        # [4] Филтър за проследимост — код (факти/GDPR) + лека паралелна LLM проверка само
        # за rules/forbidden.md на всеки кандидат (виж докстринга на verifier.py защо това
        # вече не е "без LLM извикване" — затваря pre-selection пропуск от план 15).
        log.info("[4] Филтър за проследимост (код + лека проверка на правила)...")
        verifications = verifier.check_all(post_dir, candidates, briefing_text, pool, knowledge, client, force=force)
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
    formatted = formatting.enforce(
        official_text,
        effective_rules,
        brief_text=briefing_text,
        has_photos=bool(media_selection.selected),
        founded_year=founded_year,
        today=today,
    )
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

    # research-plan-34-speed-quality-second-pass.md (Част 4): Media Picker досега подбираше
    # снимки без преценка дали пасват тематично на брифа — открито на живо (хакатон бриф,
    # избрани общи networking снимки без коментар). open_notes, не warnings — несъответствие
    # на снимка изисква преглед, не е GDPR-клас задължителна спирачка.
    for item in media_selection.selected:
        if item.topical_mismatch:
            reason_suffix = f": {item.mismatch_reason}" if item.mismatch_reason else ""
            open_notes.append(
                f"Media Picker флагна '{item.filename}' като вероятно несъответстваща тематично "
                f"на брифа{reason_suffix} — прегледайте дали снимката пасва."
            )

    if plan.is_sensitive:
        open_notes.append(
            "ЧУВСТВИТЕЛНА ТЕМА — Планерът прецени, че тя изисква преглед и одобрение преди "
            "публикуване (CLAUDE.md: автономност хибрид — рутинните автономно, чувствителните "
            "с одобрение). Прегледайте лично, дори ако няма други бележки по-долу."
        )

    status = "ready_with_notes" if (open_notes or warnings) else "ready"
    log.info("Финален статус: %s (%d открити въпроса, %d предупреждения)", status, len(open_notes), len(warnings))
    for note in open_notes:
        log.warning("Открит въпрос: %s", note)

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
        extra_meta={**_ai_act_meta(), "final_check": {"status": "pending"}},
    )

    # [7] Финален критик — АСИНХРОННО (план 15, Част 4/1.1: тази стъпка сама изяжда ~70s в
    # v3, без да променя изхода — чисто информативна и там). Виж _start_async_final_check
    # защо не пише през споделения logger.
    thread = _start_async_final_check(
        post_dir, client, knowledge, plan, briefing_text, official_text, on_final_check=on_final_check
    )
    if wait_for_final_check_seconds > 0:
        result = _await_final_check(thread, post_dir, result, wait_for_final_check_seconds, log)

    return result


def _start_async_final_check(
    post_dir,
    client,
    knowledge,
    plan,
    briefing_text,
    official_text,
    *,
    findings: list[str] | None = None,
    on_final_check: Callable[[FinalCheckUpdate], None] | None = None,
) -> threading.Thread:
    """
    Стъпка [7], извън критичния път — тече във фонов daemon thread, за да не блокира вече
    показания на човека резултат. Споделена между v3 и v5 (v3 я вика с `findings`, за да
    получи и resolution_summary — план 13; v5 не подава findings, само бележките).

    `on_final_check`: извикан в края на `_worker()`, СЛЕД `_merge_async_notes_into_meta` —
    диска се обновява първо, викането на callback-а е допълнително, никога заместващо.
    Обвито в try/except: счупен callback (грешка в TUI/REPL кода на викащия) не бива да
    заличи вече записаното в meta.json, нито да остави необработено изключение в daemon
    thread, което Python просто мълчаливо изяжда (виж стандартната библиотека) — по-добре
    явен ред в run.log, отколкото тих провал без следа.

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

    def _notify(update: FinalCheckUpdate) -> None:
        if on_final_check is None:
            return
        try:
            on_final_check(update)
        except Exception as exc:
            _append_run_log(post_dir / "run.log", f"[7] on_final_check се счупи: {type(exc).__name__}: {exc}")

    def _worker() -> None:
        log_path = post_dir / "run.log"
        started = time.monotonic()
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

            # Само v3 подава findings (Критикът на Post I) — v5 няма аналог (пише N
            # независими кандидата, не Post I → Post II), затова там resolution_summary
            # остава None, точно както преди.
            resolution_summary = None
            if findings:
                resolution_summary = _resolution_summary(len(findings), len(final_notes))
                _append_run_log(log_path, f"[7] {resolution_summary}")

            duration_seconds = round(time.monotonic() - started, 2)
            _merge_async_notes_into_meta(
                post_dir,
                final_notes,
                resolution_summary=resolution_summary,
                final_check={"status": "ok", "duration_seconds": duration_seconds},
            )
            _notify(
                FinalCheckUpdate(
                    post_dir=post_dir, status="ok", notes=final_notes, resolution_summary=resolution_summary
                )
            )
        except AgentError as exc:
            duration_seconds = round(time.monotonic() - started, 2)
            _append_run_log(log_path, f"[7] Финалната проверка (async) се счупи: {exc.human_message}")
            _merge_async_notes_into_meta(
                post_dir,
                [],
                final_check={"status": "failed", "error": exc.human_message, "duration_seconds": duration_seconds},
            )
            _notify(FinalCheckUpdate(post_dir=post_dir, status="failed", error=exc.human_message))
        except Exception as exc:
            # Живо тестване (план 17, А5) хвана AttributeError от развален JSON парсинг —
            # преди тук минаваше суров traceback в конзолата на потребителя и стъпка [7]
            # оставаше без никаква следа в meta.json. Резюме, не пълен traceback — виж
            # докстринга на _append_run_log защо тук няма [ERROR] ниво.
            duration_seconds = round(time.monotonic() - started, 2)
            error_summary = f"{type(exc).__name__}: {exc}"
            _append_run_log(log_path, f"[7] Финалната проверка (async) се счупи неочаквано: {error_summary}")
            _merge_async_notes_into_meta(
                post_dir,
                [],
                final_check={"status": "failed", "error": error_summary, "duration_seconds": duration_seconds},
            )
            _notify(FinalCheckUpdate(post_dir=post_dir, status="failed", error=error_summary))

    thread = threading.Thread(target=_worker, daemon=True, name=f"final-check-{post_dir.name}")
    thread.start()
    return thread


def _await_final_check(
    thread: threading.Thread, post_dir: Path, result: PipelineResult, timeout_seconds: float, log
) -> PipelineResult:
    """
    Best-effort изчакване на стъпка [7] за caller-и, които могат да си позволят да блокират
    (виж докстринга на run_pipeline, `wait_for_final_check_seconds`) — за разлика от REPL/chat,
    еднократно CLI извикване не убива никого другиго, ако отговори няколко секунди по-бавно.

    Ако нишката завърши в срока: препрочита output/meta.json (вече обновен от
    `_merge_async_notes_into_meta` в самата нишка) и връща НОВ `PipelineResult` с пресни
    open_notes/status/resolution_summary — вместо мутация на място, за да остане очевидно
    в diff-а на извикващия код, че резултатът може да се различава от подадения.
    Ако не завърши в срока: връща `result` непроменен — нишката продължава на фон като
    daemon (текущото поведение), просто този caller спира да я чака.
    """
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        log.info(
            "[7] Финалната проверка не завърши в рамките на %.0fs изчакване — продължава на "
            "фон, резултатът тук е без нея.",
            timeout_seconds,
        )
        return result
    meta_path = post_dir / "output" / "meta.json"
    try:
        meta = read_json(meta_path)
    except (OSError, ValueError):
        return result
    return dataclasses.replace(
        result,
        status=meta.get("status", result.status),
        open_notes=list(meta.get("open_notes", result.open_notes)),
        resolution_summary=meta.get("resolution_summary", result.resolution_summary),
    )


def _append_run_log(log_path: Path, message: str) -> None:
    """Пряк запис във файл, без logging модула — виж докстринга на _start_async_final_check."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} [INFO] {message}\n")


def _merge_async_notes_into_meta(
    post_dir: Path,
    final_notes: list[str],
    *,
    resolution_summary: str | None = None,
    final_check: dict[str, Any] | None = None,
) -> None:
    """
    Дописва находките от асинхронната проверка в output/meta.json СЛЕД като той вече е
    записан от consolidator.finalize() по-горе — read-merge-write, за да не се загуби
    останалата одитна следа. bench.py и всеки друг читател на meta.json вижда пълната
    картина, щом фоновата проверка приключи. TUI/REPL, които вече са показали резултата на
    екрана, НЕ се обновяват визуално — известно, документирано ограничение (план 15,
    Недоказани твърдения), не пропуск.

    `resolution_summary` (само от v3, виж _start_async_final_check): записва се дори когато
    `final_notes` е празен списък — точно тогава е най-интересен ("напълно адресирани").

    `final_check` (виж research-plan-18-critique-of-plan-17.md, А5): явен статус на стъпка
    [7] — {"status": "ok"} или {"status": "failed", "error": "..."}. Записва се дори при
    0 находки, за да различи "проверих, чисто е" от "не успях да проверя" (иначе двете
    изглеждат еднакво отвън — липса на нова бележка). От research-plan-35-critique-of-plan-34.md
    (Критика 2) носи и `"duration_seconds"` — реалната продължителност на стъпка [7], измерена
    директно в `_worker()`, вместо да се предполага от разлика в wall-clock часовници между
    отделни пускания (ненадеждно — маскирано от паралелизма между Писателя и Media Picker).
    """
    meta_path = post_dir / "output" / "meta.json"
    if not meta_path.exists() or (not final_notes and resolution_summary is None and final_check is None):
        return
    try:
        meta = read_json(meta_path)
    except (OSError, ValueError):
        return
    if final_notes:
        meta["open_notes"] = list(meta.get("open_notes", [])) + list(final_notes)
        if meta.get("status") == "ready":
            meta["status"] = "ready_with_notes"
    if resolution_summary is not None:
        meta["resolution_summary"] = resolution_summary
    if final_check is not None:
        meta["final_check"] = final_check
    write_json(meta_path, meta)
