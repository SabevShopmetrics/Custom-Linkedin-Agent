"""
Стъпка 6 — Консолидатор. От 2026-08-11 това вече не е механично чистене на един текст,
а СИНТЕЗ: получава Версия I, Версия II и находките на Критика върху Версия I, и съставя
официалното предложение — за всяко твърдение по-силната от двете формулировки.

Защо от двете, а не просто "по-новата": Версия II е писана срещу списък със слабости и
затова понякога преиграва — губи сполучлив начален ред или топъл детайл, който Версия I е
имала. Изборът "винаги последната чернова" изхвърля това безмълвно. Изборът "по-силното от
двете" го запазва, а находките на Критика са филтърът, който пази да не се върне заедно с
него и проблемът.

Стъпката вика модел с преценка (Role.CONSOLIDATOR — вижте .env: моделът за тази роля вече
трябва да е от класа на Писателя, не "nothink" вариант, защото задачата вече не е
форматиране). Ако втори пост няма (Критикът не е намерил нищо за запълване), стъпката се
свежда до старото механично чистене на Версия I — виж CLEANUP_SYSTEM_PROMPT.

meta.json носи целия одит: статус, открити въпроси след консолидацията, предупреждения,
плана, кои чернови са влезли в предложението, снимките и всяко LLM извикване.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import MediaSelectionSchema, PlanSchema
from src.pipeline.util import read_text_or_none, write_json, write_text

MERGE_SYSTEM_PROMPT = """\
Ти си Консолидаторът в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Получаваш две
версии на един и същи пост и списък със слабостите, които Критикът намери във Версия I.
Съставяш ОФИЦИАЛНОТО предложение — един текст, готов за публикуване.

ВАЖНО за посоката на работа: Версия II е писана срещу списък от слабости и точно затова
често преиграва — става суха и административна, губи топлината, конкретните ползи и
живото начало, които Версия I е имала. Версия I пък съдържа онова, което Критикът е
посочил. Нито една от двете НЕ е готовият отговор. Работата ти е да вземеш силата на
всяка.

Мини през поста част по част и решавай за всяка отделно:
1. Начало (първи ред/абзац) — кое хваща по-добре, без да нарушава посочените слабости.
2. Основен абзац — кое обяснява поводa по-конкретно и проследимо.
3. Списък с акценти — кои точки са по-конкретни и не се повтарят.
4. Затваряне — кое звучи по-естествено в гласа на клъстера.

Правила:
- Нито една от посочените слабости не бива да присъства в резултата. Ако по-хубавата
  формулировка е точно онази, която Критикът е посочил — вземи смисъла ѝ, но я преформулирай.
- НЕ добавяй факти, имена, числа или дати, които не присъстват в поне една от двете версии.
  Ти избираш и съединяваш, не пишеш нов пост и не разширяваш.
- Ако двете версии си противоречат по факт, вземи онази, която Критикът НЕ е оспорил.
- Да върнеш едната версия дословно НЕ е приемлив отговор, освен ако другата е по-слаба във
  всяка от четирите части. Почти никога не е така.
- Резултатът е един свързан текст, а не колаж от двата — редактирай преходите, за да върви
  гладко. Не повтаряй един и същ факт (число, дата) в две различни части.
- Без хаштагове (гласът на клъстера не ги ползва), без заглавия, без markdown ограждане,
  без обяснения за избора си.

Върни само финалния текст на поста, нищо друго.
"""

CLEANUP_SYSTEM_PROMPT = """\
Ти си Консолидатор. Получаваш финален текст на LinkedIn пост. Механична задача: изчисти
двойни празни редове и излишни интервали. НЕ добавяй хаштагове — гласът на клъстера не ги
ползва (виж knowledge/voice/formats.md). НЕ променяй смисъла, НЕ добавяй и НЕ маха
съдържание. Върни само финалния текст, нищо друго.
"""


@dataclass
class PipelineResult:
    status: str  # "ready" | "ready_with_notes" — конвейерът винаги стига до предложение
    output_dir: Path
    post_path: Path
    meta_path: Path
    warnings: list[str] = field(default_factory=list)
    open_notes: list[str] = field(default_factory=list)
    merged_from: list[str] = field(default_factory=list)
    applied_fixes: list[str] = field(default_factory=list)


def synthesize(
    post_dir: Path,
    client: LLMClient,
    *,
    first_post: str,
    second_post: str | None,
    findings: list[str],
    force: bool = False,
) -> tuple[str, list[str]]:
    """
    Записва output/post.md и връща (официален текст, кои чернови са влезли в него).

    Разделено от `finalize`, защото статусът и откритите въпроси се знаят едва СЛЕД като
    предложението съществува — Критикът прави последна информативна проверка върху него
    (виж runner.py). Един метод, който пише и поста, и meta.json, би принудил статусът да
    се гадае преди проверката.
    """
    post_path = post_dir / "output" / "post.md"
    merged_from = ["post_i.md"] + (["post_ii.md"] if second_post else [])

    if post_path.exists() and not force:
        return (read_text_or_none(post_path) or "").strip(), merged_from

    if second_post:
        findings_text = "\n".join(f"- {finding}" for finding in findings) or "(няма изрични находки)"
        prompt = (
            f"Версия I:\n{first_post}\n\n"
            f"Версия II:\n{second_post}\n\n"
            f"Слабости, намерени във Версия I:\n{findings_text}\n\n"
            f"Състави официалното предложение."
        )
        system = MERGE_SYSTEM_PROMPT
    else:
        prompt = first_post
        system = CLEANUP_SYSTEM_PROMPT

    official = client.complete(Role.CONSOLIDATOR, prompt, system=system)
    assert isinstance(official, str)
    write_text(post_path, official.strip() + "\n")
    return official.strip(), merged_from


def _normalized(text: str) -> str:
    return " ".join(text.split())


def synthesis_bypassed(official_text: str, first_post: str, second_post: str | None) -> str | None:
    """
    Връща името на версията, ако предложението е дословно копие на една от двете — иначе None.

    Наблюдавано при първото реално пускане на новата верига (2026-08-11): Консолидаторът
    върна Версия II буквално, дума по дума. Не е грешка, която гърми — изходът изглежда
    напълно нормален — но синтезът, заради който съществува стъпката, не се е случил, и
    точно топлината на Версия I, която финалната проверка после поиска, е била под ръка.
    Затова случаят се открива в кода и излиза като открит въпрос, вместо да мине тихо.
    """
    if second_post and _normalized(official_text) == _normalized(second_post):
        return "post_ii.md"
    if _normalized(official_text) == _normalized(first_post):
        return "post_i.md"
    return None


def finalize(
    post_dir: Path,
    client: LLMClient,
    plan: PlanSchema,
    media_selection: MediaSelectionSchema,
    *,
    no_photos: bool,
    status: str,
    open_notes: list[str] | None = None,
    warnings: list[str] | None = None,
    merged_from: list[str] | None = None,
    applied_fixes: list[str] | None = None,
) -> PipelineResult:
    """Копира избраните снимки и записва output/meta.json — пълната одитна следа."""
    output_dir = post_dir / "output"
    post_path = output_dir / "post.md"
    meta_path = output_dir / "meta.json"

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
        "open_notes": open_notes or [],
        "warnings": warnings or [],
        "merged_from": merged_from or [],
        "applied_fixes": applied_fixes or [],
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
        status=status,
        output_dir=output_dir,
        post_path=post_path,
        meta_path=meta_path,
        warnings=warnings or [],
        open_notes=open_notes or [],
        merged_from=merged_from or [],
        applied_fixes=applied_fixes or [],
    )
