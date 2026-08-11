"""Стъпка 1 — Планер. Решава тип пост, формат, тон, нужда от снимки. Пише plan.json."""

from __future__ import annotations

from pathlib import Path

from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import PlanSchema
from src.pipeline.util import list_photos, read_json, should_skip, write_json

SYSTEM_PROMPT_TEMPLATE = """\
Ти си Планер в конвейер за генериране на LinkedIn постове за ИКТ Клъстер – Варна.

Контекст за организацията:

{brand_context}

Задачата ти: прочети брифа за повод и реши как трябва да изглежда постът — не пишеш
самия текст, само стратегията за него. Разгледай внимателно има ли отметка за
чувствителност или бележка, изискваща човешко одобрение.

Какво знаем за формата на фирмена страница (проучване, август 2026 — виж
docs/research-plans/research-plan-10-linkedin-post-craft.md): пост само с текст стига до
съществено по-малко хора от пост със снимка или документ/карусел. Затова:
- Ако в photos/ има снимки — избирай "текст+снимка", освен ако поводът наистина не търпи
  изображение.
- "карусел" е за съдържание с 3+ отделни точки, които стоят по-добре като слайдове
  (обучение, обзор, няколко профила) — но само ако брифът дава достатъчно материал.
- "само текст" е последен избор, не подразбиране. Ако го избереш при налични снимки,
  обясни защо в notes.

Върни САМО един JSON обект със следните полета, без никакъв текст извън него:
{{
  "post_type": "кратко определение на типа повод",
  "format": "текст+снимка | само текст | карусел",
  "tone": "кратко описание на желания тон за точно този пост",
  "needs_photos": true/false,
  "max_photo_count": цяло число 0-10,
  "is_sensitive": true/false,
  "notes": "кратки бележки за писателя и media picker-а"
}}
"""


def run(post_dir: Path, client: LLMClient, brand_context: str, briefing_text: str, *, force: bool = False) -> PlanSchema:
    plan_path = post_dir / "plan.json"
    if should_skip(plan_path, force=force):
        return PlanSchema.model_validate(read_json(plan_path))

    photos = list_photos(post_dir / "photos")
    photo_note = f"В photos/ има {len(photos)} снимка(и)." if photos else "Папката photos/ е празна — няма снимки."

    prompt = f"{briefing_text}\n\n---\n{photo_note}"
    system = SYSTEM_PROMPT_TEMPLATE.format(brand_context=brand_context or "(няма зареден brand контекст)")

    plan = client.complete(Role.PLANNER, prompt, system=system, schema=PlanSchema)
    assert isinstance(plan, PlanSchema)
    write_json(plan_path, plan.model_dump())
    return plan
