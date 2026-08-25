"""
Стъпка 2 — Писател (post_i.md) и стъпка 5 — втори пост по находките на Критика (post_ii.md).
И двете използват същата роля/модел (Role.WRITER) — вижте src/llm/roles.py.

От 2026-08-11 стъпка 5 не е "ревизия" (поправка на място на същия текст), а ВТОРИ
пълноценен пост по същия повод, който запълва посочените слабости. Разликата не е
терминологична: ревизията се стреми да пипа минимално и наследява структурата на
първия текст заедно с проблемите му; вторият пост има право да преструктурира. И двата
остават на диска, защото Консолидаторът съставя официалното предложение от двата
(виж src/pipeline/consolidator.py и docs/technical/process-3.md).

От 2026-08-12 (план 11, Фаза 0, находка 2.6): Писателят получава facts.yaml и списъка
членове, които може да назове. Дотогава тези два източника влизаха само в промпта на
Критика — тоест корпусът се използваше, за да се хване Писателят, че не е конкретен, но
не и за да му се даде с какво да бъде. При тънък бриф това му оставяше три възможности:
да измисли (забранено), да премълчи (къс пост) или да напише общо. Мерено на 20 реални
пускания, случваше се третото.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from src.brand.loader import BrandKnowledge
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import PlanSchema
from src.pipeline.facts_pool import FactsPool, render_for_writer
from src.pipeline.util import read_text_or_none, should_skip, write_text

DRAFT_SYSTEM_PROMPT_TEMPLATE = """\
Ти си Писателят в конвейер за генериране на LinkedIn постове за ИКТ Клъстер – Варна.
Пишеш финалния текст на поста — точно както ще изглежда публикуван, нищо друго
(без обяснения, без markdown заглавия, без хаштагове — гласът на клъстера не ги ползва).

Контекст за организацията, глас и стил:

{brand_context}

{sources_block}

Планът за този конкретен пост:
- тип: {post_type}
- формат: {format}
- тон: {tone}
- бележки: {notes}

Пиши на база брифа по-долу и ИЗТОЧНИЦИТЕ по-горе — не измисляй факти, имена или числа,
които не са в нито едно от двете.

КАК РАБОТИ LINKEDIN (изведено от проучване, август 2026 — виж
docs/research-plans/research-plan-10-linkedin-post-craft.md). Където това се разминава с
ТОН/ФОРМАТИ по-горе, важи корпусът, не тези редове:

- Първите ~140 знака са всичко, което се вижда преди „виж повече". Първият ред трябва да
  стои сам като изречение и да носи конкретния факт (число, дата, място) — не въведение,
  не поздрав, не общо твърдение. Читател, който не разгърне, трябва пак да е разбрал повода.
- Постът трябва да върши работа БЕЗ клик. Не пиши текст, който е само подводка към линк.
- НИКОГА не измисляй адрес. Пиши линк само ако точният адрес е в брифа или в корпуса.
  Ако нямаш точен адрес, НЕ пиши затваряща фраза от типа "вижте повече тук" и НЕ оставяй
  placeholder като "[линк]" — видим счупен маркер в готов пост изглежда непрофесионално,
  все едно постът е забравен недовършен. Просто завърши без такъв ред.
  Измислен домейн е най-скъпата грешка тук: изглежда достоверно и се публикува.
- Без въдици за ангажимент („Съгласни ли сте? Коментирайте по-долу", „Пишете 👇") — от 2026
  LinkedIn изрично понижава такива постове.
- НИКОГА не обяснявай в самия пост защо нещо липсва („не мога да публикувам имената поради
  GDPR", „по правни/поверителност причини не споменаваме..."). Читателят не бива да вижда
  вътрешната кухня на писането. Ако не можеш да назовеш някого/нещо — просто пиши общо
  („фирми членове", „партньорска гимназия") и мълчи за причината; ако причината заслужава
  внимание на човек, тя е бележка ЗАД кулисите (конвейерът я показва отделно), не изречение
  в текста на поста.
- Преди да върнеш текста, преброй емоджитата: трябва да са 3-6 (виж ТОН/ФОРМАТИ), всяко на
  структурна позиция (bullet, благодарност, препратка) — не по средата на изречение и не като
  декорация в отварящото изречение. Ако си под 3, добави тематично емоджи на legitimate
  структурен елемент, който вече пишеш (не добавяй нов, изкуствен ред само заради бройката).
- Пиши за конкретна аудитория (студенти, фирми членове, партньори), не за всички наведнъж.
- Плътност: всяко изречение носи факт — число, име, дата, място, конкретен резултат.
  Изречение, което може да стои в пост на всяка друга организация („фокусирахме се върху
  ключови етапи", „с цел максимална полза"), е празно и не бива да го има. По-добре с
  едно изречение по-малко, отколкото с едно празно повече.
- Дължината НЕ е фиксирана цел — прецени я сам по количеството реално съдържание в брифа
  и ИЗТОЧНИЦИТЕ. Ако брифът дава достатъчно имена за благодарност, числа, ход на събитието
  или резултати — пиши дълго, свободно до горната граница от ТОН/ФОРМАТИ по-горе. Ако брифът
  е тънък, спри се на долната граница, но НЕ добавяй общи изречения само за да „запълниш"
  диапазона — плътността от точката по-горе има предимство пред бройката думи.
"""

SOURCES_BLOCK_TEMPLATE = """\
ИЗТОЧНИЦИ ЗА ПРОСЛЕДИМИ ФАКТИ. Всяко число, име, дата, програма или адрес в поста трябва да
идва от брифа или оттук. Това не е списък за изчерпване — вземай само относимото към повода.
Каквото не е нито в брифа, нито тук, не съществува и не се пише.

{facts_block}

{members_block}\
"""


def render_sources_block(knowledge: BrandKnowledge) -> str:
    """
    Фактическите източници, форматирани за Писателя.

    Различно от същите блокове в critic.py по предназначение: там въпросът е „проследимо ли
    е това твърдение", тук — „с какво имам право да бъда конкретен". Затова списъкът с
    членове е обърнат: Критикът вижда всички (за да ги хване), Писателят вижда само тези с
    потвърдено съгласие. Име, което не е пред очите му, е име, което няма да сложи.
    """
    # Блок 1: facts.yaml дословно като YAML — програми, адрес, ЕИК и т.н. Ако липсва,
    # изричен ред казва на модела да не разчита на нищо извън брифа.
    if knowledge.facts_available and knowledge.facts:
        facts_yaml = yaml.safe_dump(knowledge.facts, allow_unicode=True, sort_keys=False)
        facts_block = f"## ФАКТИ ЗА ОРГАНИЗАЦИЯТА (identity/facts.yaml)\n\n```yaml\n{facts_yaml}```"
    else:
        facts_block = (
            "## ФАКТИ ЗА ОРГАНИЗАЦИЯТА\n\n"
            "facts.yaml липсва или е празен — брифът е единственият ти източник за факти."
        )

    # Блок 2: само членовете с mention_allowed: true — обратно на critic.py, който вижда
    # всички (за да хване неразрешено споменаване).
    mentionable = [
        str(member.get("name", "")).strip()
        for member in knowledge.members
        if member.get("mention_allowed") is True and member.get("name")
    ]
    header = "## СПОМЕНАВАНЕ НА ОРГАНИЗАЦИИ ПО ИМЕ"
    rule = (
        "Правилото е едно: **име, което не е в брифа, не влиза в поста.** Брифът е писан от "
        "човек, който отговаря за него — щом е сложил името, ползвай го. Обратното НЕ важи: "
        "не изваждай имена от корпуса или от еталонните примери, за да обогатиш текста. "
        "Когато трябва да споменеш организация, която не е в брифа, пиши обобщено: "
        "'фирми членове', 'партньорска гимназия', 'университет партньор'."
    )
    if mentionable:
        names = "\n".join(f"- {name}" for name in mentionable)
        members_block = (
            f"{header}\n\n{rule}\n\n"
            f"Членове с изрично потвърдено съгласие (mention_allowed: true) — тях може да "
            f"назовеш и когато не са в брифа, ако поводът наистина ги засяга:\n{names}"
        )
    else:
        members_block = (
            f"{header}\n\n{rule}\n\n"
            "Нито един член няма потвърдено съгласие (mention_allowed: true) в members.yaml, "
            "затова няма име, което да добавиш по своя преценка."
        )

    return SOURCES_BLOCK_TEMPLATE.format(facts_block=facts_block, members_block=members_block)

SECOND_POST_SYSTEM_PROMPT_TEMPLATE = """\
Ти си същият Писател. Пишеш ВТОРА версия на същия пост. Първата версия вече съществува и
Критикът е изброил какво ѝ липсва или куца.

Задачата ти НЕ е козметична поправка на първата версия. Задачата ти е втори пълноценен
пост по същия повод, който запълва посочените слабости. Имаш право да преструктурираш —
друг начален ред, друг ред на изреченията, друга затваряща формула — стига поводът, фактите
и гласът на клъстера да останат същите. Не измисляй факти, имена или числа, които не са в
брифа или в първата версия.

И двете версии остават — след теб Консолидаторът съставя официалното предложение от двете.
Затова не се опитвай да "спасиш" формулировка от първата версия, ако Критикът я е посочил:
напиши по-добрата, а изборът между двете не е твой.

ВАЖНО: находките на Критика са ВЪТРЕШНИ инструкции само за теб — те никога не се появяват
в самия пост. Не пиши неща като "⚠️ GDPR предупреждение...", "(бележка: ...)" или каквото и
да е позоваване на процеса на проверка в текста на поста. Ако находка не изисква промяна в
самия текст, просто я игнорирай мълчаливо — не я споменавай никъде.

Върни само финалния текст на поста, нищо друго — без хаштагове.

Контекст за организацията, глас и стил:

{brand_context}

{sources_block}
"""


def write_first_draft(
    post_dir: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    briefing_text: str,
    *,
    force: bool = False,
    mode_instruction: str = "",
) -> str:
    """Пише Post I (стъпка 2) — първата пълноценна чернова на поста по плана и брифа.

    `mode_instruction` идва от pipeline/modes.py (режим "Кадър"/"Фокус"/"Панорама", избран
    изрично от човека) — "" при "Фокус" означава без промяна спрямо диапазона от корпуса.
    """
    draft_path = post_dir / "post_i.md"
    if should_skip(draft_path, force=force):
        # Възобновяемост: вече има чернова от предишно пускане.
        return read_text_or_none(draft_path) or ""

    # Системният промпт носи корпуса + факти/членове + плана; брифът остава чист user prompt.
    system = DRAFT_SYSTEM_PROMPT_TEMPLATE.format(
        brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
        sources_block=render_sources_block(knowledge),
        post_type=plan.post_type,
        format=plan.format,
        tone=plan.tone,
        notes=plan.notes or "(няма)",
    )
    if mode_instruction:
        system += f"\n\n{mode_instruction}\n"
    text = client.complete(Role.WRITER, briefing_text, system=system)
    assert isinstance(text, str)  # без schema=, complete() винаги връща str тук
    write_text(draft_path, text.strip() + "\n")
    return text


def write_second_post(
    post_dir: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    briefing_text: str,
    first_post_text: str,
    findings: list[str],
    *,
    output_filename: str = "post_ii.md",
    force: bool = False,
    mode_instruction: str = "",
) -> str:
    """Втори пост по същия повод, който запълва находките на Критика върху първия.

    `mode_instruction` — виж докстринга на write_first_draft; Post II пази същия избран
    режим за дължина като Post I, иначе Консолидаторът би обединявал два поста с несъвместими
    цели за обем.
    """
    second_path = post_dir / output_filename
    if should_skip(second_path, force=force):
        # Възобновяемост: вече има Post II от предишно пускане.
        return read_text_or_none(second_path) or ""

    # User промптът носи цялата верига: оригинален бриф + Post I + находките на Критика,
    # форматирани като списък със задачи за запълване.
    findings_text = "\n".join(f"- {finding}" for finding in findings) or "(няма изрични находки)"
    prompt = (
        f"Оригинален бриф:\n{briefing_text}\n\n"
        f"Версия I (написана вече):\n{first_post_text}\n\n"
        f"Какво Критикът намери във Версия I:\n{findings_text}\n\n"
        f"Напиши Версия II — същият повод, без изброените слабости."
    )
    system = SECOND_POST_SYSTEM_PROMPT_TEMPLATE.format(
        brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
        sources_block=render_sources_block(knowledge),
    )
    if mode_instruction:
        system += f"\n\n{mode_instruction}\n"

    text = client.complete(Role.WRITER, prompt, system=system)
    assert isinstance(text, str)
    write_text(second_path, text.strip() + "\n")
    return text


# --------------------------------------------------------------------- v5 (кандидати, план 15)

# Един ред "ъгъл" на кандидат вместо temperature (process-4.md, стъпка 2: "температура
# и/или един ред ъгъл в промпта"). Съзнателно избрано пред temperature: llm/client.py вече
# подава temperature само ако е зададена в .env (план 11, находка 2.7) и не е тествано как
# self-hosted доставчикът реагира на вариране на нея за диверсификация — вижте план 15,
# Недоказани твърдения. Ъгълът е детерминиран, безплатен и не зависи от поведение на
# доставчик, което не е измерено.
CANDIDATE_ANGLES = [
    "пиши от гледна точка на екипа на клъстера, който организира повода",
    "пиши от гледна точка на човек, който обмисля да участва — какво получава той конкретно",
    "пиши от гледна точка на фирма член/партньор — защо поводът е релевантен за бизнеса",
]


def write_candidates(
    post_dir: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    briefing_text: str,
    facts_pool: FactsPool,
    *,
    n: int = 3,
    force: bool = False,
    mode_instruction: str = "",
) -> list[tuple[str, str]]:
    """
    Стъпка [2] на v5 — N кандидата, паралелно, независими (process-4.md: "вторият не
    наследява свиването на първия" — точно обратното на v3's Post I → Post II верига).

    Връща [(filename, text), ...] в стабилен ред (post_1.md, post_2.md, ...), независимо
    от реда, в който паралелните извиквания реално приключват.
    """
    candidates_dir = post_dir / "candidates"
    slots = [(f"post_{i + 1}.md", CANDIDATE_ANGLES[i % len(CANDIDATE_ANGLES)]) for i in range(n)]

    results: dict[str, str] = {}
    to_generate: list[tuple[str, str]] = []
    for filename, angle in slots:
        path = candidates_dir / filename
        if should_skip(path, force=force):
            # Възобновяемост: вече има този кандидат от предишно пускане.
            results[filename] = read_text_or_none(path) or ""
        else:
            to_generate.append((filename, angle))

    if to_generate:
        with ThreadPoolExecutor(max_workers=len(to_generate)) as executor:
            futures = {
                executor.submit(
                    _write_one_candidate,
                    candidates_dir / filename,
                    client,
                    knowledge,
                    plan,
                    briefing_text,
                    facts_pool,
                    angle,
                    mode_instruction,
                ): filename
                for filename, angle in to_generate
            }
            for future, filename in futures.items():
                results[filename] = future.result()

    return [(filename, results[filename]) for filename, _ in slots]


def _write_one_candidate(
    path: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    briefing_text: str,
    facts_pool: FactsPool,
    angle: str,
    mode_instruction: str = "",
) -> str:
    """
    Един кандидат. Преизползва DRAFT_SYSTEM_PROMPT_TEMPLATE (същата LinkedIn-занаятчийска
    част като v3's Post I) — разликата е `sources_block`: явният факт-регистър вместо
    сурови YAML файлове (виж facts_pool.render_for_writer), плюс един ред за ъгъла и
    евентуален режим за дължина (pipeline/modes.py).
    """
    system = (
        DRAFT_SYSTEM_PROMPT_TEMPLATE.format(
            brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
            sources_block=render_for_writer(facts_pool),
            post_type=plan.post_type,
            format=plan.format,
            tone=plan.tone,
            notes=plan.notes or "(няма)",
        )
        + f"\n\nГледна точка за тази версия (не променя факти, само фокус/ред на изреченията): {angle}\n"
    )
    if mode_instruction:
        system += f"\n{mode_instruction}\n"
    text = client.complete(Role.WRITER, briefing_text, system=system)
    assert isinstance(text, str)
    write_text(path, text.strip() + "\n")
    return text
