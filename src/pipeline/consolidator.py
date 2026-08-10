"""
Стъпка 6 — Консолидатор. Механично обединява финалния текст, копира избраните
снимки и записва output/meta.json с пълния одитен лог. Използва "nothink" модела —
тук не се очаква преценка, само форматиране.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import MediaSelectionSchema, PlanSchema
from src.pipeline.util import write_json, write_text

SYSTEM_PROMPT = """\
Ти си Консолидатор. Получаваш финален текст на LinkedIn пост. Механична задача: изчисти
двойни празни редове и излишни интервали. НЕ добавяй хаштагове — гласът на клъстера не ги
ползва (виж knowledge/voice/formats.md). НЕ променяй смисъла, НЕ добавяй и НЕ маха
съдържание. Върни само финалния текст, нищо друго.
"""


@dataclass
class PipelineResult:
    status: str  # "ready" | "needs_human"
    output_dir: Path
    post_path: Path
    meta_path: Path
    warnings: list[str] = field(default_factory=list)


def run(
    post_dir: Path,
    client: LLMClient,
    plan: PlanSchema,
    media_selection: MediaSelectionSchema,
    final_text: str,
    *,
    no_photos: bool,
    status: str = "ready",
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    force: bool = False,
) -> PipelineResult:
    output_dir = post_dir / "output"
    post_path = output_dir / "post.md"
    meta_path = output_dir / "meta.json"

    if not post_path.exists() or force:
        cleaned = client.complete(Role.CONSOLIDATOR, final_text, system=SYSTEM_PROMPT)
        assert isinstance(cleaned, str)
        write_text(post_path, cleaned.strip() + "\n")

    selected_dir = output_dir / "selected"
    selected_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    for item in media_selection.selected:
        source = post_dir / "photos" / item.filename
        if source.exists():
            shutil.copy2(source, selected_dir / source.name)
            copied.append(source.name)
        else:
            missing.append(item.filename)

    meta = {
        "status": status,
        "needs_human_reasons": reasons or [],
        "warnings": warnings or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": plan.model_dump(),
        "photos": {
            "no_photos_in_source": no_photos,
            "selected": copied,
            "missing_referenced_by_media_picker": missing,
        },
        "calls": [entry.to_dict() for entry in client.call_log],
    }
    write_json(meta_path, meta)

    return PipelineResult(
        status=status, output_dir=output_dir, post_path=post_path, meta_path=meta_path, warnings=warnings or []
    )
