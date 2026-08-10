"""
Стъпка 2 — Писател (post_i.md) и стъпка 5 — ревизия след критика (post_ii.md).
И двете използват същата роля/модел (Role.WRITER) — вижте src/llm/roles.py.
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
(без обяснения, без markdown заглавия, само самия пост с хаштагове в края).

Контекст за организацията, глас и стил:

{brand_context}

Планът за този конкретен пост:
- тип: {post_type}
- формат: {format}
- тон: {tone}
- бележки: {notes}

Пиши само на база брифа по-долу — не измисляй факти, имена или числа, които не са в него.
"""

REVISION_SYSTEM_PROMPT_TEMPLATE = """\
Ти си същият Писател, но сега преработваш чернова по бележки от Критика. Запази гласа
и структурата, поправи само каквото Критикът е посочил. Върни само финалния текст на
поста, нищо друго.

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


def write_revision(
    post_dir: Path,
    client: LLMClient,
    brand_context: str,
    plan: PlanSchema,
    briefing_text: str,
    draft_text: str,
    issues: list[str],
    *,
    output_filename: str = "post_ii.md",
    force: bool = False,
) -> str:
    revised_path = post_dir / output_filename
    if should_skip(revised_path, force=force):
        return read_text_or_none(revised_path) or ""

    issues_text = "\n".join(f"- {issue}" for issue in issues) or "(няма изрични бележки)"
    prompt = (
        f"Оригинален бриф:\n{briefing_text}\n\n"
        f"Текуща чернова:\n{draft_text}\n\n"
        f"Бележки от Критика:\n{issues_text}\n\n"
        f"Пренапиши поста, като адресираш бележките."
    )
    system = REVISION_SYSTEM_PROMPT_TEMPLATE.format(brand_context=brand_context or "(няма зареден brand контекст)")

    text = client.complete(Role.WRITER, prompt, system=system)
    assert isinstance(text, str)
    write_text(revised_path, text.strip() + "\n")
    return text
