"""
Стъпка 4 — Критик. Три отделни проверки, всяка срещу конкретен източник (виж
docs/prompts/followup-prompt-2-knowledge-critic.md, раздел 4, преразгледано в
docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md):

  - факти      → briefing.md + identity/facts.yaml + members/members.yaml
  - тон        → voice/tone.md + voice/vocabulary.md
  - правила    → rules/forbidden.md + rules/gdpr.md

От 2026-08-11 (потребителска инструкция): Критикът НЕ отхвърля постове. Нито една негова
присъда не спира конвейера. Целта му е да намери слабостите на Post I, за да напише
Писателят втори пост, който ги запълва, а Консолидаторът да състави официалното
предложение от двата. Тоест находките му са ДНЕВЕН РЕД за следващата стъпка, не вето.
`verdict` остава в схемата, но вече е само етикет за сериозност (виж
collect_findings и src/pipeline/runner.py). Предишното поведение — до 2 опита за
пренаписване, после needs_human, и твърд отказ при forbidden — е премахнато; вижте
docs/technical/process-3.md.

GDPR не се оставя изцяло на преценката на модела — две проверки се прилагат
детерминирано в кода (enforce_code_level_rules), винаги, независимо какво е върнал
LLM-ят: споменаване на организация без потвърдено mention_allowed, и лично име в текста,
което не идва от брифа. (Третата, снимка с вероятно непълнолетни, е в media_picker.py/
runner.py.) Тези находки също влизат в дневния ред на втория пост — организация без
потвърдено съгласие се маха от текста, не се "предупреждава" и оставя.

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
from src.llm.schema import CritiqueSchema, FactSource, ForbiddenCheckSchema, PlanSchema, RuleViolation, Verdict
from src.pipeline.util import read_text_or_none, should_skip, write_text

CRITIC_PRINCIPLE = (
    "Провери всяко фактическо твърдение в текста. За всяко посочи източника: брифа, "
    "facts.yaml или members.yaml. Ако твърдение не може да се проследи до нито един от "
    "тези три източника — маркирай го като недоказано. Не поправяй сам и не измисляй "
    "заместващ факт. Само посочи.\n\n"
    "Специален случай, лесен за пропускане: ВСЯКА добавена подробност, залепена към иначе "
    "реално, проследимо име (X) — текущ статус ('в момента тече подготовка за X', 'предстои "
    "скоро'), МЯСТО ('училището в град Y'), ТИТЛА, БРОЙКА или ДРУГ атрибут — е ОТДЕЛНО "
    "твърдение от самото X, с нужда от собствен източник. Че X съществува в брифа/facts.yaml/"
    "members.yaml НЕ доказва нищо за подробността, залепена до него. Реален случай "
    "(2026-08-26): текст правилно спомена реално училище от members.yaml, но добави '...в "
    "град Кюстендил' — град, който не съществува никъде в брифа или корпуса, чиста добавка. "
    "Провери РАЗДЕЛНО: първо дали X е познато, после дали ВСЯКА подробност около X също е."
)

SYSTEM_PROMPT_TEMPLATE = """\
Ти си Критикът в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Правиш ТРИ отделни
проверки върху чернова, всяка срещу конкретен, посочен по-долу източник — не срещу
собствената си преценка какво звучи вярно.

ЗАДАЧАТА ТИ НЕ Е ДА ОТХВЪРЛЯШ. Постът не се спира от твоята присъда. Каквото намериш,
отива като дневен ред при Писателя, който ще напише ВТОРА версия, запълваща точно тези
слабости; после Консолидаторът съставя официалното предложение от двете версии. Затова
пиши находките конкретно и приложимо — "изразът X е клише, замени го с факт от брифа",
не "тонът е незадоволителен". Празен списък от находки значи "няма какво да се запълни",
а не "не си направил проверката".

{principle}

Моделът не проверява дали нещо е вярно в света — проверява дали е проследимо до подаден
източник. Всичко останало е халюцинация по дефиниция.

КАКВО НЕ Е ТВОЯ РАБОТА. Механичните правила се налагат от кода, преди текстът да стигне до
човек: хаштагове, кои емоджи са разрешени и колко, дължина в думи, маркери на списъка,
непознати адреси. НЕ пиши находки за тях — те вече са поправени или отбелязани отделно, а
твоята бележка само дублира. Особено важно: НЕ изброявай „разрешени емоджи" по памет. При
реален тест на 2026-08-11 такъв списък беше цитиран с един измислен символ в него — точно
затова тези проверки минаха в кода. Твоята работа е преценка: проследимост на фактите, тон
срещу корпуса, правила по същество.

Контекст за организацията, глас, стил и правила:

{brand_context}

{facts_block}

{members_block}

Планът за този пост:
- тип: {post_type}
- тон: {tone}
- чувствителен: {is_sensitive}

{planner_notes_block}

Направи ЧЕТИРИТЕ проверки:
1. ФАКТИ — за всяко конкретно твърдение (число, име, дата, факт) в текста, посочи от кой
   от трите източника идва (briefing / facts.yaml / members.yaml), или UNVERIFIED ако не
   идва от никой.
2. ТОН — сверй срещу секциите ТОН/ТЕРМИНОЛОГИЯ по-горе. Всяко несъответствие описвай
   така, че вторият пост да може да го поправи: кой израз, защо е проблем, каква е
   посоката за замяна.
3. ПРАВИЛА — сверй срещу секциите ЗАБРАНЕНО/GDPR по-горе. Нарушение на ЗАБРАНЕНО
   (rule: "forbidden") е най-сериозната находка — тя ЗАДЪЛЖИТЕЛНО трябва да отпадне
   напълно във втората версия, не да се смекчи. Нарушение на GDPR (rule: "gdpr") също
   влиза в rule_violations и също се маха от текста (напр. организация без потвърдено
   съгласие просто не се споменава).
4. БЕЛЕЖКА НА ПЛАНЕРА — ако бележката по-горе флагва конкретен проблем (напр. невъзможна
   дата, невярна годишнина, липсваща критична информация), провери дали чернова ГО Е
   РЕШИЛА, не само дали е "проследимо до брифа" — брифът може буквално да съдържа грешка
   (потребителят е написал невярна дата), в който случай проверка 1 (ФАКТИ) я пропуска,
   защото формално идва от брифа. Ако проблемът стои непроменен в текста, това е
   ЗАДЪЛЖИТЕЛНА находка в tone_issues, дори да няма друг проблем с твърдението.

Върни САМО JSON обект:
{{
  "verdict": "ok | needs_revision | hard_reject",
  "facts": [{{"claim": "...", "source": "briefing | facts.yaml | members.yaml | UNVERIFIED", "note": "..."}}],
  "tone_issues": ["..."],
  "rule_violations": [{{"rule": "gdpr | forbidden", "detail": "..."}}],
  "summary": "едно изречение обобщение"
}}

verdict е само етикет за сериозност, НЕ решение дали постът да излезе: "hard_reject" САМО
ако има поне едно rule_violations с rule: "forbidden"; "needs_revision", ако има UNVERIFIED
факти или tone_issues; иначе "ok". GDPR находки сами по себе си не вдигат verdict до
"hard_reject".

ВАЖНО за валиден JSON: никога не влагай кавички (нито ASCII ", нито типографски „..." или
"...") вътре в стойност на низ, дори когато цитираш фраза от текста или даваш пример за
формулировка. Ползвай единични кавички ' или просто перифразирай без кавички. Всяка
кавичка вътре в JSON низ, която не е екранирана с \", чупи целия отговор.
"""

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Груба евристика за лично име (Име Фамилия, две последователни капитализирани кирилски
# думи) — НЕ заменя човешка преценка, само хваща очевидни случаи за флагване.
#
# [ \t]+ нарочно, НЕ \s+ — \s+ матчва и нови редове, което по-рано хващаше фалшиво
# последната дума на един абзац + първата на следващия (двете капитализирани, защото
# едната е край на изречение, другата начало на ново) като измислено "лично име". Открито
# при реално end-to-end тестване на 2026-08-10 ("Клъстера" + "Благодарим" на нов ред).
_NAME_PATTERN = re.compile(r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)[ \t]+([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\b")

# Институционни/административни глави на фрази, изключени от Проверка 2 — реален фалшив
# положителен, потвърден ДВАЖДИ при живо тестване (2026-08-28, 2026-08-31, виж
# research-plan-24): "Община Варна" съвпада перфектно с "Име Фамилия" формата на
# _NAME_PATTERN отгоре, но е институция, не лице. Списъкът е нарочно тесен — само
# съществителни, които на български не могат да бъдат нечие първо име (за разлика от
# генерично разширяване, което би могло да скрие истински личен GDPR риск).
_INSTITUTION_HEAD_WORDS = {
    "община", "министерство", "областна", "общинска", "районна", "агенция",
    "дирекция", "камара", "палата", "асоциация", "сдружение", "фондация",
}

# Географски/посочни прилагателни — същият клас фалшив положителен, но за региони, не
# институции. Потвърдено ДВА пъти в едно живо тестване (2026-08-31, виж
# research-plan-25-adversarial-live-testing-findings.md): "Северна Европа" и
# "Североизточна България" (последното вече беше документирано като фалшив положителен на
# СЪСЕДНАТА Проверка 3 в research-plan-18 — тук се повтаря на Проверка 2, защото тя все още
# ползва генеричния двудумен модел). Прилагателно за посока на български никога не е нечие
# първо име.
_GEOGRAPHIC_HEAD_WORDS = {
    "северна", "южна", "източна", "западна", "централна",
    "североизточна", "северозападна", "югоизточна", "югозападна",
}

# Проверка 3 (виж research-plan-18-critique-of-plan-17.md, А1): организация-подобен низ,
# ограничена до по-сигурни сигнали — латиница или изрична правна форма. НЕ ползва
# генеричния "две капитализирани кирилски думи" модел на _NAME_PATTERN по-горе за самата
# организация: калибрация върху 150 реални поста показа реален фалшив положителен точно
# на този модел ("Северноизточна България" — регион, не организация/лице), затова обхватът
# в тази фаза е нарочно по-тесен, докато не се събере повече евиденция за чисто кирилски
# организационни имена без правна форма.
_LATIN_ORG_PATTERN = re.compile(r"\b([A-Z][a-zA-Z]+(?:[ \t]+[A-Z][a-zA-Z]+){1,3})\b")
_ORG_SUFFIX_PATTERN = re.compile(
    r"\b([А-ЯЁA-Z][\w-]*(?:[ \t]+[А-ЯЁA-Z][\w-]*){0,3}[ \t]+(?:ООД|ЕООД|АД|ЕАД|КД|СД|Ltd|GmbH|Inc|LLC|Corp)\.?)\b"
)

_PAREN_RE = re.compile(r"\(([^)]+)\)")
# Немалко записи ползват кавички вместо скоби за псевдонима, напр.
# ПГКМКС "Академик Благовест Сендов" — поддържа както права, така и българска отваряща.
_QUOTE_RE = re.compile(r'[„"]([^„"“]+)["“]')
# Водеща абревиатура от поне 4 главни кирилски букви (ПГКМКС, ГПЧЕ, ВВМУ...) — реален
# пропуск, открит на 2026-08-18: пост съкращава "ПГКМКС "Академик Благовест Сендов""
# до "ПГКМКС – Варна", което не съдържа нито пълното име, нито псевдонима в кавички.
# Под 4 букви НЕ се добавя нарочно (СУ, ОУ, ПГ, МГ...) — множество различни членове в
# members.yaml споделят такива кратки съкращения и биха дали грешно съвпадение.
_LEADING_ACRONYM_RE = re.compile(r"^([А-ЯЁ]{4,})\b")

# Правна форма в края на официалното име ("Зариба ЕООД") — реален пост естествено пропуска
# я ("Зариба"), виж докстринга на _member_name_variants за живия случай, който я разкри.
_LEGAL_SUFFIX_RE = re.compile(r"\s+(ЕООД|ООД|АД|ЕАД|КД|СД)\.?$", re.IGNORECASE)


def _member_name_variants(name: str) -> list[str]:
    """
    Записите в members.yaml често са "Пълно официално име (Популярно/английско име)"
    (напр. "Зариба ЕООД (Zariba)") — пост естествено споменава само късата форма
    ("Zariba"), която НИКОГА не е точна подниза на пълния низ. Проверка само срещу
    целия `name` пропуска почти всеки реален случай. Затова проверяваме и частите: псевдоним
    в скоби, псевдоним в кавички и водеща абревиатура (виж коментарите над regex-ите по-горе).

    "before"-текстът пред скоби/кавички (напр. "ОбУ" в 'ОбУ „Христо Ботев"') е ПОЧТИ ВИНАГИ
    институционален префикс (ОУ, СУ, МГ, ОбУ, ГПЧЕ...), не бранд — точно затова водещата
    абревиатура вече имаше отделен праг от 4+ букви (виж _LEADING_ACRONYM_RE по-горе: "Под 4
    букви НЕ се добавя нарочно... биха дали грешно съвпадение"). "before"-клоновете тук обаче
    не минаваха през същия праг, само през общия >= 3 — открито при живо тестване 2026-08-26:
    "ОбУ" (3 букви, members.yaml) съвпадна с началото на най-обикновената дума "обучение" във
    всеки пост за уъркшоп/обучение, лепейки фалшив GDPR въпрос на текст, който изобщо не
    споменава училището. Затова "before"-текстът пази същия 4+ праг като акронима; пълните
    псевдоними в скоби/кавички (напр. "TCV", "Zariba" — бранд имена, не префикси) остават на 3+.

    Обратният пропуск, открит СЪЩИЯ ден на живо тестване: "Зариба ЕООД (Zariba)" — реален
    пост естествено пише само "Зариба" (кирилица, без правната форма), който не съвпада НИТО
    с "Zariba" (латиница), НИТО с "before"-варианта "Зариба ЕООД" (изисква и "ЕООД" да следва).
    Постът мина изцяло невидян за GDPR проверката. Затова "before"-текстът се проверява и БЕЗ
    правната си форма (ЕООД/ООД/АД/ЕАД/КД/СД) — засяга поне 5 реални записа в members.yaml
    (Зариба, Ронева, Фенси-студио, АйДиЕс Глобъл, Роял Технолоджи).
    """
    variants = [name.strip()]
    short_prefixes: list[str] = []

    paren_match = _PAREN_RE.search(name)
    if paren_match:
        alias = paren_match.group(1).strip()
        if alias:
            variants.append(alias)
        before = name[: paren_match.start()].strip()
        if before:
            short_prefixes.append(before)
            bare = _LEGAL_SUFFIX_RE.sub("", before).strip()
            if bare and bare != before:
                variants.append(bare)

    quote_match = _QUOTE_RE.search(name)
    if quote_match:
        alias = quote_match.group(1).strip()
        if alias:
            variants.append(alias)
        before = name[: quote_match.start()].strip()
        if before:
            short_prefixes.append(before)

    acronym_match = _LEADING_ACRONYM_RE.match(name)
    if acronym_match:
        short_prefixes.append(acronym_match.group(1))

    variants.extend(v for v in short_prefixes if len(v) >= 4)

    return [v for v in variants if len(v) >= 3]  # твърде кратки низове дават фалшиви положителни


_HYPHEN_SPLIT_RE = re.compile(r"\s+-\s+")


def _own_org_name_variants(own_org_names: list[str] | None) -> list[str]:
    """
    Точни варианти на собственото име на организацията (виж enforce_code_level_rules,
    Проверка 3) — за разлика от _member_name_variants(), НЕ прави подниза-съвпадение по
    време на проверка (виж бележката там защо), само разбива facts.yaml's "ICT Cluster -
    Varna" на думи-разделени-с-тире-и-интервали ПРЕДИ сравнението: ["ICT Cluster - Varna",
    "ICT Cluster", "Varna"] — точни варианти в множеството, не гъвкав regex по време на
    самата проверка.
    """
    variants: list[str] = []
    for name in own_org_names or []:
        if not name:
            continue
        variants.append(name)
        parts = [p.strip() for p in _HYPHEN_SPLIT_RE.split(name) if p.strip()]
        if len(parts) > 1:
            variants.extend(parts)
    return [v for v in variants if len(v) >= 3]


def _render_planner_notes_block(plan: PlanSchema) -> str:
    """
    Бележката на Планера (`plan.notes`) — досега стигаше само до Писателя (writer.py), не и
    до Критика. Реален пропуск, хванат при живо тестване (2026-08-26): бриф с невъзможна
    дата ("30 февруари") и невярна годишнина ("200-годишнина" за организация от 2008 г.) —
    Планерът правилно разпозна и двете грешки в бележката си ("Задължителна човешка
    корекция преди писане"), но Писателят ги написа въпреки това, а Критикът никога не видя
    бележката, за да провери дали са останали — финалният "готов" пост излезе с невъзможна
    дата и измислена годишнина, без нито едно предупреждение към човека. Проверка 4 по-долу
    затваря точно тази дупка.
    """
    if not plan.notes.strip():
        return "## БЕЛЕЖКА НА ПЛАНЕРА\n\n(няма)"
    return f"## БЕЛЕЖКА НА ПЛАНЕРА (написана преди чернова — провери дали е адресирана)\n\n{plan.notes.strip()}"


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
    """
    Извиква Критика върху `draft_text` и записва резултата в `filename`. Ползва се два пъти
    в runner.py: върху Post I (critique.md, стъпка 4, находките хранят Post II) и върху
    официалното предложение (critique_final.md, стъпка 7, чисто информативно).
    """
    critique_path = post_dir / filename
    if should_skip(critique_path, force=force):
        # Възобновяемост: вече има записана критика — препрочети вградения JSON блок.
        existing = read_text_or_none(critique_path) or ""
        return _parse_critique_markdown(existing)

    # Критикът вижда ВСИЧКИ членове (за разлика от Писателя) — трябва да може да хване
    # споменаване на организация без потвърдено съгласие.
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
        planner_notes_block=_render_planner_notes_block(plan),
    )
    prompt = f"Бриф:\n{briefing_text}\n\nЧернова за проверка:\n{draft_text}"

    critique = client.complete(Role.CRITIC, prompt, system=system, schema=CritiqueSchema)
    assert isinstance(critique, CritiqueSchema)

    # Детерминираните GDPR проверки на код ниво идват СЛЕД LLM отговора и мутират
    # критиката — независимо какво е решил моделът (виж докстринга на функцията).
    organization = (knowledge.facts or {}).get("organization") or {}
    own_org_names = [organization.get("name_bg") or "", organization.get("name_en") or ""]
    enforce_code_level_rules(critique, draft_text, briefing_text, knowledge.members, own_org_names)

    write_text(critique_path, _render_critique_markdown(critique))
    return critique


def _render_facts_block(knowledge: BrandKnowledge) -> str:
    """facts.yaml дословно като YAML в промпта на Критика, или изрична бележка, че липсва."""
    if not knowledge.facts_available or not knowledge.facts:
        return (
            "## FACTS.YAML\n\n"
            "facts.yaml липсва или е празен — работиш САМО с брифа като източник за факти. "
            "Отбележи изрично в summary, че фактическата проверка е частична."
        )
    facts_yaml = yaml.safe_dump(knowledge.facts, allow_unicode=True, sort_keys=False)
    return f"## FACTS.YAML\n\n```yaml\n{facts_yaml}```"


def _render_members_block(knowledge: BrandKnowledge) -> str:
    """Пълният списък членове + mention_allowed за всеки — Критикът трябва да ги вижда всичките."""
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
    own_org_names: list[str] | None = None,
) -> None:
    """
    Прилага в кода двете GDPR проверки от раздел 6 на followup-prompt-2, независимо от
    това какво е върнал LLM-ят. Мутира `critique` на място: добавя rule_violations.

    От 2026-08-10: GDPR находки (rule="gdpr") НЕ вдигат verdict до HARD_REJECT — само
    "forbidden" го прави (от rules/forbidden.md, вкл. вече върнати от LLM-а такива).
    От 2026-08-11 нито едната стойност не спира конвейера — verdict е етикет за
    сериозност, който подрежда находките в дневния ред на втория пост (collect_findings).

    Забележка: проверката за "лично име извън брифа" е груба регулярна евристика
    (две последователни капитализирани кирилски думи), не истинска NER — целта е да
    хване очевидни случаи, не да замести човешка преценка.

    `own_org_names` (виж research-plan-19-critique-of-phase1-implementation.md, А-1):
    официалните имена на самата организация (facts.yaml — name_bg/name_en), изключени от
    Проверка 3, за да не флагне собственото си име, когато е изписано на латиница (напр.
    "ICT Cluster" от "ICT Cluster - Varna") — Проверка 3 иначе не знае нищо за собствената
    самоличност на клъстера, само за трети страни (`members`).
    """
    violations = list(critique.rule_violations)
    lower_draft = draft_text.lower()

    # Проверка 1: организация, спомената в текста, чието mention_allowed не е true.
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

    # Проверка 2: низ, който прилича на лично име (виж _NAME_PATTERN), но не е дословно в брифа.
    for match in _NAME_PATTERN.finditer(draft_text):
        candidate = match.group(0)
        if match.group(1).lower() in _INSTITUTION_HEAD_WORDS or match.group(1).lower() in _GEOGRAPHIC_HEAD_WORDS:
            continue  # "Община Варна", "Северна Европа" и т.н. — не лично име (research-plan-24/25)
        if candidate not in briefing_text:
            violations.append(
                RuleViolation(rule="gdpr", detail=f"Възможно лично име извън брифа: '{candidate}'.")
            )

    # Проверка 3: организация-подобен низ (латиница или с правна форма — виж бележката над
    # _LATIN_ORG_PATTERN за защо не и генерично кирилско), който не е нито познат член
    # (Проверка 1 вече го покрива, независимо от mention_allowed), нито (виж own_org_names
    # по-горе) самата организация.
    #
    # НАРОЧНО без изключение "candidate дословно в брифа" (за разлика от Проверка 2 за лични
    # имена) — фикс, направен при живо тестване на 2026-08-27 (виж research-plan-23): и двата
    # реални примера от План 17, които мотивираха тази проверка (Marina Tech Solutions,
    # Neurolink Systems GmbH), идваха ДИРЕКТНО от брифа. По-ранна версия имаше точно това
    # изключение (копирано по аналогия от Проверка 2) и мълчаливо пропускаше двата случая,
    # заради които изобщо се пише тази проверка — потвърдено чрез реално пускане с брифа
    # 'Благодарим на TechVenture GmbH...' Съгласие на трета страна за публично асоцииране с
    # клъстера не се установява от факта, че човек е написал името ѝ в брифа (за разлика от
    # лично име, при което операторът вероятно познава човека) — затова тук проверката е
    # безусловна спрямо брифа, огледално на Проверка 1 (членове), не на Проверка 2 (имена).
    #
    # Точно равенство (не подниза) за ДВЕТЕ множества — известни членове И собствено име —
    # нарочно: по-ранна версия правеше "candidate е подниза на познато име", за да покрие
    # "ICT Cluster" срещу "ICT Cluster - Varna" (виж research-plan-19), но подниза-проверка
    # срещу members би потискала различна, реално непозната организация, чието име случайно
    # споделя дума с познат член (напр. непознатата "Tech Solutions" срещу познатия член
    # "Marina Tech Solutions" — фалшиво отрицание на реален случай, не на собственото име).
    # Затова "ICT Cluster - Varna" се разбива на думи/тирета в own_org_name_variants() ПРЕДИ
    # сравнението — точни варианти в множеството, не гъвкаво съвпадение по време на проверка.
    #
    # Известен, ПРИЕТ остатъчен риск (виж research-plan-21-critique-of-round2-and-latin-
    # branding-risk.md): Писателят понякога измисля английска под-марка за собствена програма
    # на клъстера, извън и брифа, и facts.yaml буквално — реален пример от архива, "AI Edition"
    # (за "ТЕХНОСТАРТ Weekend", facts.yaml описва само "AI хакатон", кирилица). own_org_names
    # НЕ хваща това (низът не съществува никъде в корпуса за сравнение), а по-агресивна
    # евристика (контекстова сигнална дума) риска да пропусне истински случаи (Marina Tech
    # Solutions/Neurolink Systems GmbH от План 17 нямат гарантирана сигнална дума до себе си).
    # Оставено нарочно непроменено — GDPR категорията е предупреждение, никога отказ (виж
    # verdict логиката по-долу), цената на този клас фалшив положителен вече е приета за ниска,
    # същия принцип, на който разчита Проверка 1/2 от самото начало.
    known_org_variants = {
        variant.lower() for member in members for variant in _member_name_variants(member.get("name") or "")
    }
    known_org_variants.update(v.lower() for v in _own_org_name_variants(own_org_names))
    flagged_orgs: set[str] = set()
    for pattern in (_LATIN_ORG_PATTERN, _ORG_SUFFIX_PATTERN):
        for match in pattern.finditer(draft_text):
            candidate = match.group(0)
            key = candidate.lower()
            if key in known_org_variants or key in flagged_orgs:
                continue
            flagged_orgs.add(key)
            violations.append(
                RuleViolation(
                    rule="gdpr",
                    detail=(
                        f"Възможна непозната организация извън members.yaml: '{candidate}' — "
                        f"провери съгласие за публично споменаване ръчно, преди публикуване."
                    ),
                )
            )

    critique.rule_violations = violations
    # "forbidden" винаги вдига HARD_REJECT; "gdpr" никога — виж докстринга на модула защо.
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


FORBIDDEN_ONLY_SYSTEM_PROMPT_TEMPLATE = """\
Ти си бърза, тясна проверка в конвейер за LinkedIn постове на ИКТ Клъстер – Варна — НЕ пълният
Критик. Единствената ти задача: провери подадения текст САМО срещу секцията "ЗАБРАНЕНО" в
корпуса по-долу (критика или сравнение с конкретна поименно назована фирма/организация,
политическа позиция, религиозна позиция, здравен/финансов/правен съвет представен като
препоръка на клъстера, статистика извън източник, идентифициращ детайл за непълнолетно лице
без документирано съгласие). НЕ проверявай факти, тон или организация без mention_allowed —
те се проверяват другаде в конвейера, извън тази роля. Празен списък означава "няма
нарушение", не "не съм проверил".

{brand_context}

Върни САМО JSON:
{{"rule_violations": [{{"rule": "forbidden", "detail": "..."}}]}}

ВАЖНО за валиден JSON: никога не влагай кавички (нито ASCII ", нито типографски „..." или
"...") вътре в стойност на низ. Ползвай единични кавички ' или перифразирай без кавички.
"""


def check_forbidden_rules(client: LLMClient, knowledge: BrandKnowledge, text: str) -> list[RuleViolation]:
    """
    Стъпка [4] на v5 (src/pipeline/verifier.py) — лека, ЕДИНСТВЕНО-правило проверка на ЕДИН
    кандидат, ПРЕДИ Съдията да избере измежду тях.

    Затваря пропуск, документиран в докстринга на verifier.py: v5 нарочно не вика пълния
    Критик (тази LLM роля, но с факти+тон+правила накуп) на кандидат — целта на цялата верига
    (research-plan-15) — затова досега нарушение на rules/forbidden.md (напр. критика на
    конкретен конкурент) не се хващаше преди Съдията да избере, само асинхронно СЛЕД избора
    (стъпка [7], информативно). Тази функция е нарочно много по-лека от пълния Критик (само
    ЗАБРАНЕНО, без факти/тон/GDPR) — за N паралелни кандидата остава евтина, не връща целия
    v3 разход, който план 15 се опитваше да премахне.

    GDPR НЕ е тук — остава изцяло код-ниво (enforce_code_level_rules), непроменено; резултатът
    се филтрира изрично до rule != "gdpr" в случай моделът все пак върне такъв ред.
    """
    system = FORBIDDEN_ONLY_SYSTEM_PROMPT_TEMPLATE.format(
        brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
    )
    result = client.complete(Role.CRITIC, text, system=system, schema=ForbiddenCheckSchema)
    assert isinstance(result, ForbiddenCheckSchema)
    return [v for v in result.rule_violations if v.rule != "gdpr"]


def gdpr_warnings(critique: CritiqueSchema) -> list[str]:
    """GDPR находки за показване като предупреждения, не като спирачи на конвейера."""
    return [v.detail for v in critique.rule_violations if v.rule == "gdpr"]


def review_notes(critique: CritiqueSchema) -> list[str]:
    """
    Същите находки, но формулирани за ЧОВЕК, не за Писателя — това е списъкът „какво
    остава открито в предложението", който излиза в meta.json, конзолата и терминалната
    сесия след финалната проверка. Отделно от collect_findings, защото публиката е друга:
    Писателят получава инструкции ("махни X"), човекът получава констатации ("X остава").
    """
    notes: list[str] = []
    for violation in critique.rule_violations:
        label = "GDPR" if violation.rule == "gdpr" else "rules/forbidden.md"
        notes.append(f"[{label}] {violation.detail}")
    notes.extend(critique.tone_issues)
    for fact in critique.facts:
        if fact.source is FactSource.UNVERIFIED:
            notes.append(f"Недоказано твърдение: \"{fact.claim}\" ({fact.note or 'няма източник'})")
    return notes


def collect_findings(critique: CritiqueSchema) -> list[str]:
    """
    Превръща критиката в дневен ред за втория пост — какво точно да запълни.

    Редът не е случаен: най-напред онова, което ЗАДЪЛЖИТЕЛНО трябва да отпадне
    (нарушени правила), после собствената препоръка на Критика, после тонът и накрая
    недоказаните твърдения. Препоръката (summary) идва преди детайлите, защото Критикът
    често вече е формулирал решението ("формулирай благодарността структурно, без имена"),
    а не само проблема — без нея вторият пост гадае посоката наново.

    Празен списък значи "няма какво да се запълни" → runner.py пропуска втория пост и
    консолидира само първия (виж там). Затова тази функция е и решението дали стъпка 5
    се изпълнява — не отделен флаг, който може да се разсинхронизира с находките.
    """
    findings: list[str] = []

    # Ред 1: нарушени правила — най-сериозното, трябва да отпадне напълно.
    for violation in critique.rule_violations:
        if violation.rule == "gdpr":
            findings.append(
                f"GDPR — махни това от текста, не го смекчавай: {violation.detail}"
            )
        else:
            findings.append(
                f"Нарушено правило от rules/forbidden.md — трябва да отпадне НАПЪЛНО: {violation.detail}"
            )

    # Ред 2: обобщената препоръка на Критика — често вече съдържа посоката на решение.
    if critique.summary:
        findings.append(f"Препоръка на Критика: {critique.summary}")

    # Ред 3: тонови проблеми, дословно от Критика.
    findings.extend(critique.tone_issues)

    # Ред 4: недоказани твърдения — последни, защото поправката им е най-евтина (махни/провери).
    for fact in critique.facts:
        if fact.source is FactSource.UNVERIFIED:
            findings.append(
                f"Недоказано твърдение — или го проследи до брифа/корпуса, или го премахни: "
                f"\"{fact.claim}\" ({fact.note or 'няма източник'})"
            )

    return findings


def _render_critique_markdown(critique: CritiqueSchema) -> str:
    """Човекочетим critique.md с вграден machine-readable JSON блок за _parse_critique_markdown."""
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


def load_critique(path: Path) -> CritiqueSchema | None:
    """
    Прочита записана критика от диск, или None ако файлът липсва/е неразчетим.

    За разлика от `run`, тук липсващият файл НЕ е грешка: анализът на минали пускания
    (src/quality/bench.py) минава през папки, в които стъпка 4 или 7 може да не е стигнала
    да запише нищо, и това е самата информация, която се търси.
    """
    text = read_text_or_none(path)
    if not text:
        return None
    try:
        return _parse_critique_markdown(text)
    except (ValueError, KeyError):
        return None


def _parse_critique_markdown(text: str) -> CritiqueSchema:
    """Обратното на _render_critique_markdown — препрочита вградения JSON при skip/load_critique."""
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError("critique файлът не съдържа очаквания вграден JSON блок за препрочитане.")
    return CritiqueSchema.model_validate(json.loads(match.group(1)))
