"""
Оркестрира шестте стъпки на конвейера и управлява възобновяемостта.

Верига: Планер → (Писател || Media Picker) → Критик → в зависимост от присъдата:
  - OK              → направо към Консолидатор
  - NEEDS_REVISION  → Писателят получава до `max_revision_attempts` опита (по подразбиране
                       2) — на всеки кръг Критикът препрочита и ако все още не е доволен,
                       конкретната му препоръка (summary) се подава директно на следващия
                       опит, не просто списък от проблеми. Ако след всички опити пак не е
                       "ok" → needs_human.
  - HARD_REJECT     → спира веднага, на който и да е кръг, БЕЗ по-нататъшни опити — само
                       за нарушение на rules/forbidden.md (виж critic.py). GDPR вече НЕ
                       предизвиква hard_reject (виж по-долу).

От 2026-08-10 (потребителска инструкция): GDPR находки (споменаване на организация без
потвърдено mention_allowed, лично име извън брифа, снимка с вероятно непълнолетни) НЕ
спират конвейера — конвейерът е инструмент за вътрешния маркетинг екип на клъстера, не
автоматично публикуване към клиенти, и човек преглежда output/post.md преди публикуване
във всички случаи. Тези находки се логват като предупреждения (WARNING в run.log и
конзолата) и се записват в output/meta.json под "warnings" — не вдигат статус до
needs_human. Виж docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md.

Всеки run пише хронологичен лог в posts/<име>/run.log (виж src/logging_utils.py) —
допълва meta.json с четим наратив на решенията по пътя.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.brand.loader import BrandKnowledge, load_brand_knowledge
from src.errors import BriefingMissingError, BriefingNotFilledError, NeedsHumanReviewError
from src.llm.schema import Verdict
from src.llm.client import LLMClient
from src.logging_utils import setup_run_logger
from src.pipeline import consolidator, critic, media_picker, planner, writer
from src.pipeline.consolidator import PipelineResult
from src.pipeline.util import read_text_or_none, should_skip, write_text

EXAMPLE_BRIEFING_PATH = Path("posts/_example/briefing.md")


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


_ROUND_SUFFIXES = ["ii", "iii", "iv", "v", "vi"]


def _round_filenames(round_index: int) -> tuple[str, str]:
    """round_index е 2 за първата ревизия, 3 за втората и т.н. (post_i.md/critique.md
    вече заемат round 1)."""
    suffix = _ROUND_SUFFIXES[round_index - 2] if round_index - 2 < len(_ROUND_SUFFIXES) else str(round_index)
    return f"post_{suffix}.md", f"critique_{suffix}.md"


def _collect_revision_notes(critique) -> list[str]:
    """
    Препоръката на Критика (summary) се подава на Писателя ПЪРВА и изрично — Критикът често
    вече е предложил конкретно решение (напр. "формулирай благодарността структурно, без
    имена"), не просто описание на проблема. Без това, ревизията гадае наново всеки път.
    """
    notes: list[str] = []
    if critique.summary:
        notes.append(f"Препоръка на Критика: {critique.summary}")
    notes.extend(critique.tone_issues)
    for fact in critique.facts:
        if fact.source.value == "UNVERIFIED":
            notes.append(f"Непотвърдено твърдение: \"{fact.claim}\" ({fact.note or 'няма източник'})")
    return notes


def _split_rule_violations(rule_violations) -> tuple[list[str], list[str]]:
    """Връща (blocking_details, gdpr_warning_details). GDPR никога не е в blocking."""
    blocking = [v.detail for v in rule_violations if v.rule != "gdpr"]
    gdpr = [v.detail for v in rule_violations if v.rule == "gdpr"]
    return blocking, gdpr


def run_pipeline(
    post_dir: Path,
    client: LLMClient,
    *,
    force: bool = False,
    knowledge: BrandKnowledge | None = None,
    max_revision_attempts: int = 2,
) -> PipelineResult:
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

    # [1] Планер
    log.info("[1] Планер...")
    plan = planner.run(post_dir, client, knowledge.stable_context, briefing_text, force=force)
    log.info("[1] Планер готово: тип=%s формат=%s чувствителен=%s", plan.post_type, plan.format, plan.is_sensitive)

    # [2] Писател и [3] Media Picker вървят паралелно — независими една от друга
    log.info("[2]/[3] Писател + Media Picker (паралелно)...")
    with ThreadPoolExecutor(max_workers=2) as executor:
        draft_future = executor.submit(
            writer.write_first_draft, post_dir, client, knowledge.stable_context, plan, briefing_text, force=force
        )
        media_future = executor.submit(media_picker.run, post_dir, client, plan, briefing_text, force=force)
        post_i_text = draft_future.result()
        media_selection, no_photos = media_future.result()
    if no_photos:
        log.warning("Папката photos/ е празна — постът ще съдържа само текст.")
    log.info("[2]/[3] готово: чернова %d символа, %d избрани снимки", len(post_i_text), len(media_selection.selected))

    reasons: list[str] = []
    warnings: list[str] = []
    immediate = False

    # [4] Критик
    log.info("[4] Критик (кръг 1)...")
    critique = critic.run(post_dir, client, knowledge, plan, briefing_text, post_i_text, force=force)
    log.info("[4] Присъда: %s", critique.verdict.value)

    if critique.verdict == Verdict.OK:
        final_text = post_i_text
        post_ii_path = post_dir / "post_ii.md"
        if should_skip(post_ii_path, force=force):
            final_text = read_text_or_none(post_ii_path) or final_text
        else:
            write_text(post_ii_path, final_text)
        _, gdpr = _split_rule_violations(critique.rule_violations)
        warnings.extend(gdpr)

    elif critique.verdict == Verdict.HARD_REJECT:
        # Правило: твърд отказ (rules/forbidden.md) не се пренаписва — минава директно на човек.
        final_text = post_i_text
        immediate = True
        blocking, gdpr = _split_rule_violations(critique.rule_violations)
        reasons.extend(blocking)
        warnings.extend(gdpr)
        log.error("[4] HARD_REJECT (forbidden): %s", "; ".join(blocking))

    else:  # NEEDS_REVISION — до max_revision_attempts опита, всеки път с конкретната
        # препоръка на предишния кръг Критик подадена директно на Писателя
        current_draft = post_i_text
        current_critique = critique
        round_index = 2

        while current_critique.verdict == Verdict.NEEDS_REVISION and round_index <= 1 + max_revision_attempts:
            revision_notes = _collect_revision_notes(current_critique)
            draft_filename, critique_filename = _round_filenames(round_index)
            log.info("[5] Ревизия (кръг %d)...", round_index)
            current_draft = writer.write_revision(
                post_dir,
                client,
                knowledge.stable_context,
                plan,
                briefing_text,
                current_draft,
                revision_notes,
                output_filename=draft_filename,
                force=force,
            )
            current_critique = critic.run(
                post_dir, client, knowledge, plan, briefing_text, current_draft, filename=critique_filename, force=force
            )
            log.info("[5] Присъда (кръг %d): %s", round_index, current_critique.verdict.value)
            round_index += 1

        final_text = current_draft
        _, gdpr = _split_rule_violations(current_critique.rule_violations)
        warnings.extend(gdpr)

        if current_critique.verdict == Verdict.HARD_REJECT:
            immediate = True
            blocking, _ = _split_rule_violations(current_critique.rule_violations)
            reasons.extend(blocking)
            log.error("[5] HARD_REJECT (forbidden) след ревизия: %s", "; ".join(blocking))
        elif current_critique.verdict == Verdict.NEEDS_REVISION:
            reasons.append(current_critique.summary or "Критикът все още не одобрява текста след всички опити за ревизия.")
            reasons.extend(current_critique.tone_issues)
            log.warning("[5] needs_human след изчерпани опити за ревизия.")
        # else OK — продължава нормално, последният post_<suffix>.md вече е записан

    if any(item.possible_minor for item in media_selection.selected):
        warning_text = (
            "Избрана снимка е маркирана от Media Picker като вероятно съдържаща непълнолетни лица — "
            "прегледайте ръчно преди публикуване (не спира конвейера)."
        )
        warnings.append(warning_text)
        log.warning(warning_text)

    for w in warnings:
        log.warning("GDPR предупреждение: %s", w)

    status = "needs_human" if reasons else "ready"
    log.info("Финален статус: %s (%d предупреждения)", status, len(warnings))

    # [6] Консолидатор — винаги пише output/, дори при needs_human, за да остане пълна следа.
    result = consolidator.run(
        post_dir,
        client,
        plan,
        media_selection,
        final_text,
        no_photos=no_photos,
        status=status,
        reasons=reasons,
        warnings=warnings,
        force=force,
    )

    if status == "needs_human":
        log.error("Спряно: needs_human. Причини: %s", "; ".join(reasons))
        raise NeedsHumanReviewError(str(post_dir), reasons, immediate=immediate)

    return result
