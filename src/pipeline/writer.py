"""
Стъпка 2 — Писател (post_i.md) и стъпка 5 — втори пост по находките на Критика (post_ii.md).
И двете използват същата роля/модел (Role.WRITER) — вижте src/llm/roles.py.

От 2026-08-11 стъпка 5 не е "ревизия" (поправка на място на същия текст), а ВТОРИ
пълноценен пост по същия повод, който запълва посочените слабости. Разликата не е
терминологична: ревизията се стреми да пипа минимално и наследява структурата на
първия текст заедно с проблемите му; вторият пост има право да преструктурира. И двата
остават на диска, защото Консолидаторът съставя официалното предложение от двата
(виж src/pipeline/consolidator.py и docs/technical/process-3.md).
"""

from __future__ import annotations

from pathlib import Path

from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import PlanSchema
from src.pipeline.util import read_text_or_none, should_skip, write_text

DRAFT_SYSTEM_PROMPT_TEMPLATE = """\
Ти си Писателят в конвейер за генериране на LinkedIn постове за ИКТ Клъстер – Варна.
Пишеш финалния текст на поста — точно както ще изглежда публикуван, нищо друго
(без обяснения, без markdown заглавия, без хаштагове — гласът на клъстера не ги ползва).

Контекст за организацията, глас и стил:

{brand_context}

Планът за този конкретен пост:
- тип: {post_type}
- формат: {format}
- тон: {tone}
- бележки: {notes}

Пиши само на база брифа по-долу — не измисляй факти, имена или числа, които не са в него.

КАК РАБОТИ LINKEDIN (изведено от проучване, август 2026 — виж
docs/research-plans/research-plan-10-linkedin-post-craft.md). Където това се разминава с
ТОН/ФОРМАТИ по-горе, важи корпусът, не тези редове:

- Първите ~140 знака са всичко, което се вижда преди „виж повече". Първият ред трябва да
  стои сам като изречение и да носи конкретния факт (число, дата, място) — не въведение,
  не поздрав, не общо твърдение. Читател, който не разгърне, трябва пак да е разбрал повода.
- Постът трябва да върши работа БЕЗ клик. Не пиши текст, който е само подводка към линк.
- НИКОГА не измисляй адрес. Пиши линк само ако точният адрес е в брифа или в корпуса.
  Ако е нужен линк, който не ти е подаден, остави точно това: [линк] — човек ще го замени.
  Измислен домейн е най-скъпата грешка тук: изглежда достоверно и се публикува.
- Без въдици за ангажимент („Съгласни ли сте? Коментирайте по-долу", „Пишете 👇") — от 2026
  LinkedIn изрично понижава такива постове.
- Пиши за конкретна аудитория (студенти, фирми членове, партньори), не за всички наведнъж.
"""

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
"""


def write_first_draft(
    post_dir: Path,
    client: LLMClient,
    brand_context: str,
    plan: PlanSchema,
    briefing_text: str,
    *,
    force: bool = False,
) -> str:
    draft_path = post_dir / "post_i.md"
    if should_skip(draft_path, force=force):
        return read_text_or_none(draft_path) or ""

    system = DRAFT_SYSTEM_PROMPT_TEMPLATE.format(
        brand_context=brand_context or "(няма зареден brand контекст)",
        post_type=plan.post_type,
        format=plan.format,
        tone=plan.tone,
        notes=plan.notes or "(няма)",
    )
    text = client.complete(Role.WRITER, briefing_text, system=system)
    assert isinstance(text, str)
    write_text(draft_path, text.strip() + "\n")
    return text


def write_second_post(
    post_dir: Path,
    client: LLMClient,
    brand_context: str,
    plan: PlanSchema,
    briefing_text: str,
    first_post_text: str,
    findings: list[str],
    *,
    output_filename: str = "post_ii.md",
    force: bool = False,
) -> str:
    """Втори пост по същия повод, който запълва находките на Критика върху първия."""
    second_path = post_dir / output_filename
    if should_skip(second_path, force=force):
        return read_text_or_none(second_path) or ""

    findings_text = "\n".join(f"- {finding}" for finding in findings) or "(няма изрични находки)"
    prompt = (
        f"Оригинален бриф:\n{briefing_text}\n\n"
        f"Версия I (написана вече):\n{first_post_text}\n\n"
        f"Какво Критикът намери във Версия I:\n{findings_text}\n\n"
        f"Напиши Версия II — същият повод, без изброените слабости."
    )
    system = SECOND_POST_SYSTEM_PROMPT_TEMPLATE.format(
        brand_context=brand_context or "(няма зареден brand контекст)"
    )

    text = client.complete(Role.WRITER, prompt, system=system)
    assert isinstance(text, str)
    write_text(second_path, text.strip() + "\n")
    return text
