"""
Стъпка 4 — Критик. Три отделни проверки, всяка срещу конкретен източник (виж
docs/prompts/followup-prompt-2-knowledge-critic.md, раздел 4, преразгледано в
docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md):

  - факти      → briefing.md + identity/facts.yaml + members/members.yaml
  - тон        → voice/tone.md + voice/vocabulary.md
  - правила    → rules/forbidden.md (ТВЪРД ОТКАЗ, не подлежи на пренаписване) +
                 rules/gdpr.md (ПРЕДУПРЕЖДЕНИЕ, не спира конвейера — виж по-долу)

GDPR не се оставя изцяло на преценката на модела — две проверки се прилагат
детерминирано в кода (enforce_code_level_rules), винаги, независимо какво е върнал
LLM-ят: споменаване на организация без потвърдено mention_allowed, и лично име в текста,
което не идва от брифа. (Третата, снимка с вероятно непълнолетни, е в media_picker.py/
runner.py.)

От 2026-08-10 (потребителска инструкция — инструментът е за вътрешния маркетинг екип на
клъстера, не за клиенти, и няма автоматично публикуване): GDPR находки НЕ спират
конвейера и НЕ вдигат verdict до hard_reject — само rules/forbidden.md (политическа
неутралност, критика на партньори и т.н.) остава твърд отказ. GDPR находките се пишат в
critique.md, логват се като предупреждения и се показват в meta.json — човекът, който
преглежда output/post.md преди публикуване, е крайната проверка.

Съзнателно различен модел от Писателя (виж src/llm/roles.py) — модел, който критикува
собствения си текст, е склонен да го потвърди.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from src.brand.loader import BrandKnowledge
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import CritiqueSchema, PlanSchema, RuleViolation, Verdict
from src.pipeline.util import read_text_or_none, should_skip, write_text

CRITIC_PRINCIPLE = (
    "Провери всяко фактическо твърдение в текста. За всяко посочи източника: брифа, "
    "facts.yaml или members.yaml. Ако твърдение не може да се проследи до нито един от "
    "тези три източника — маркирай го като недоказано. Не поправяй сам и не измисляй "
    "заместващ факт. Само посочи."
)

SYSTEM_PROMPT_TEMPLATE = """\
Ти си Критикът в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Правиш ТРИ отделни
проверки върху чернова, всяка срещу конкретен, посочен по-долу източник — не срещу
собствената си преценка какво звучи вярно.

{principle}

Моделът не проверява дали нещо е вярно в света — проверява дали е проследимо до подаден
източник. Всичко останало е халюцинация по дефиниция.

Контекст за организацията, глас, стил и правила:

{brand_context}

{facts_block}

{members_block}

Планът за този пост:
- тип: {post_type}
- тон: {tone}
- чувствителен: {is_sensitive}

Направи трите проверки:
1. ФАКТИ — за всяко конкретно твърдение (число, име, дата, факт) в текста, посочи от кой
   от трите източника идва (briefing / facts.yaml / members.yaml), или UNVERIFIED ако не
   идва от никой.
2. ТОН — сверй срещу секциите ТОН/ТЕРМИНОЛОГИЯ по-горе. Несъответствие тук е повод за
   пренаписване, не за твърд отказ.
3. ПРАВИЛА — сверй срещу секциите ЗАБРАНЕНО/GDPR по-горе. Нарушение на ЗАБРАНЕНО
   (rule: "forbidden") е ТВЪРД ОТКАЗ, не подлежи на пренаписване. Нарушение на GDPR
   (rule: "gdpr") се отбелязва в rule_violations, но НЕ Е твърд отказ — инструментът е за
   вътрешен маркетинг екип с човешки преглед преди публикуване, не автоматично
   публикуване към клиенти.

Върни САМО JSON обект:
{{
  "verdict": "ok | needs_revision | hard_reject",
  "facts": [{{"claim": "...", "source": "briefing | facts.yaml | members.yaml | UNVERIFIED", "note": "..."}}],
  "tone_issues": ["..."],
  "rule_violations": [{{"rule": "gdpr | forbidden", "detail": "..."}}],
  "summary": "едно изречение обобщение"
}}

verdict е "hard_reject" САМО ако има поне едно rule_violations с rule: "forbidden".
GDPR находки сами по себе си не вдигат verdict до hard_reject — оставят verdict на "ok"
или "needs_revision" според факти/тон. Иначе "needs_revision", ако има UNVERIFIED факти
или tone_issues. Иначе "ok".
"""

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Груба евристика за лично име (Име Фамилия, две последователни капитализирани кирилски
# думи) — НЕ заменя човешка преценка, само хваща очевидни случаи за флагване.
_NAME_PATTERN = re.compile(r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\b")

_PAREN_RE = re.compile(r"\(([^)]+)\)")


def _member_name_variants(name: str) -> list[str]:
    """
    Записите в members.yaml често са "Пълно официално име (Популярно/английско име)"
    (напр. "Зариба ЕООД (Zariba)") — пост естествено споменава само късата форма
    ("Zariba"), която НИКОГА не е точна подниза на пълния низ. Проверка само срещу
    целия `name` пропуска почти всеки реален случай. Затова проверяваме и частите.
    """
    variants = [name.strip()]
    match = _PAREN_RE.search(name)
    if match:
        alias = match.group(1).strip()
        if alias:
            variants.append(alias)
        before = name[: match.start()].strip()
        if before:
            variants.append(before)
    return [v for v in variants if len(v) >= 3]  # твърде кратки низове дават фалшиви положителни


def run(
    post_dir: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    briefing_text: str,
    draft_text: str,
    *,
    filename: str = "critique.md",
    force: bool = False,
) -> CritiqueSchema:
    critique_path = post_dir / filename
    if should_skip(critique_path, force=force):
        existing = read_text_or_none(critique_path) or ""
        return _parse_critique_markdown(existing)

    facts_block = _render_facts_block(knowledge)
    members_block = _render_members_block(knowledge)

    system = SYSTEM_PROMPT_TEMPLATE.format(
        principle=CRITIC_PRINCIPLE,
        brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
        facts_block=facts_block,
        members_block=members_block,
        post_type=plan.post_type,
        tone=plan.tone,
        is_sensitive=plan.is_sensitive,
    )
    prompt = f"Бриф:\n{briefing_text}\n\nЧернова за проверка:\n{draft_text}"

    critique = client.complete(Role.CRITIC, prompt, system=system, schema=CritiqueSchema)
    assert isinstance(critique, CritiqueSchema)

    enforce_code_level_rules(critique, draft_text, briefing_text, knowledge.members)

    write_text(critique_path, _render_critique_markdown(critique))
    return critique


def _render_facts_block(knowledge: BrandKnowledge) -> str:
    if not knowledge.facts_available or not knowledge.facts:
        return (
            "## FACTS.YAML\n\n"
            "facts.yaml липсва или е празен — работиш САМО с брифа като източник за факти. "
            "Отбележи изрично в summary, че фактическата проверка е частична."
        )
    facts_yaml = yaml.safe_dump(knowledge.facts, allow_unicode=True, sort_keys=False)
    return f"## FACTS.YAML\n\n```yaml\n{facts_yaml}```"


def _render_members_block(knowledge: BrandKnowledge) -> str:
    if not knowledge.members:
        return "## MEMBERS.YAML\n\nmembers.yaml липсва или е празен — не сверявай членове срещу него."
    lines = [
        f"- {m.get('name', '?')} (mention_allowed: {m.get('mention_allowed')})" for m in knowledge.members
    ]
    return "## MEMBERS.YAML (само за справка кой е член — не значи автоматично разрешение за споменаване)\n\n" + "\n".join(
        lines
    )


def enforce_code_level_rules(
    critique: CritiqueSchema,
    draft_text: str,
    briefing_text: str,
    members: list[dict[str, Any]],
) -> None:
    """
    Прилага в кода двете GDPR проверки от раздел 6 на followup-prompt-2, независимо от
    това какво е върнал LLM-ят. Мутира `critique` на място: добавя rule_violations.

    От 2026-08-10: GDPR находки (rule="gdpr") НЕ вдигат verdict до HARD_REJECT — само
    "forbidden" го прави (от rules/forbidden.md, вкл. вече върнати от LLM-а такива).
    GDPR остава видимо в rule_violations/critique.md и се третира като предупреждение
    надолу по веригата (runner.py), не като спирач — виж followup-prompt-3.

    Забележка: проверката за "лично име извън брифа" е груба регулярна евристика
    (две последователни капитализирани кирилски думи), не истинска NER — целта е да
    хване очевидни случаи, не да замести човешка преценка.
    """
    violations = list(critique.rule_violations)
    lower_draft = draft_text.lower()

    for member in members:
        name = member.get("name")
        if not name or member.get("mention_allowed") is True:
            continue
        for variant in _member_name_variants(name):
            if variant.lower() in lower_draft:
                violations.append(
                    RuleViolation(
                        rule="gdpr",
                        detail=(
                            f"Организацията '{name}' е спомената в текста (като '{variant}'), но "
                            f"mention_allowed не е потвърдено (true) в members.yaml."
                        ),
                    )
                )
                break  # един violation на организация е достатъчен, не дублирай за всеки вариант

    for match in _NAME_PATTERN.finditer(draft_text):
        candidate = match.group(0)
        if candidate not in briefing_text:
            violations.append(
                RuleViolation(rule="gdpr", detail=f"Възможно лично име извън брифа: '{candidate}'.")
            )

    critique.rule_violations = violations
    has_blocking_violation = any(v.rule != "gdpr" for v in critique.rule_violations)

    if has_blocking_violation:
        critique.verdict = Verdict.HARD_REJECT
    elif critique.verdict == Verdict.HARD_REJECT:
        # LLM-ът самò е върнало hard_reject, но единствените нарушения са GDPR — политиката
        # е код-ниво, не по преценка на модела (виж followup-prompt-3), затова коригираме
        # присъдата надолу вместо да я приемем на доверие. Пресъздаваме точно правилото,
        # което промптът вече дава на модела за "без forbidden нарушения".
        has_unverified_facts = any(fact.source.value == "UNVERIFIED" for fact in critique.facts)
        critique.verdict = Verdict.NEEDS_REVISION if (critique.tone_issues or has_unverified_facts) else Verdict.OK


def gdpr_warnings(critique: CritiqueSchema) -> list[str]:
    """GDPR находки за показване като предупреждения, не като спирачи на конвейера."""
    return [v.detail for v in critique.rule_violations if v.rule == "gdpr"]


def _render_critique_markdown(critique: CritiqueSchema) -> str:
    facts_block = "\n".join(f"- \"{f.claim}\" — {f.source.value} ({f.note})" for f in critique.facts) or "(няма)"
    tone_block = "\n".join(f"- {issue}" for issue in critique.tone_issues) or "(няма)"
    rules_block = "\n".join(f"- [{v.rule}] {v.detail}" for v in critique.rule_violations) or "(няма)"
    json_block = json.dumps(critique.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return (
        f"# Критика\n\n"
        f"**Присъда:** {critique.verdict.value}\n\n"
        f"## Факти\n\n{facts_block}\n\n"
        f"## Тон\n\n{tone_block}\n\n"
        f"## Нарушени правила\n\n{rules_block}\n\n"
        f"## Обобщение\n\n{critique.summary or '(няма)'}\n\n"
        f"<!-- machine-readable, не редактирайте ръчно -->\n"
        f"```json\n{json_block}\n```\n"
    )


def _parse_critique_markdown(text: str) -> CritiqueSchema:
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError("critique файлът не съдържа очаквания вграден JSON блок за препрочитане.")
    return CritiqueSchema.model_validate(json.loads(match.group(1)))
