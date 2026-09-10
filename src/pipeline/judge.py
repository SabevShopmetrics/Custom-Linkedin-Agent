"""
Стъпка [5] (v5) — Съдия. Сравнява кандидатите ПО ДВОЙКИ и избира един — НИКОГА не пише
или редактира текст (process-4.md, стъпка 5: "Съдията няма право да върне текст").

Защо по двойки, не скаларна оценка на всеки кандидат поотделно: research-plan-15,
Част 2.4 — сравнителното класиране по двойки, по резюме на външно проучване, надминава
скаларна оценка и продължава да печели с повече кандидати, докато скаларните методи
стагнират. Точните числа са секундарни (виж план 15, Недоказани твърдения) — принципът,
не процентът, движи този избор.

За N кандидата прави N*(N-1)/2 сравнения — за подразбиращото WRITER_CANDIDATES=3 това са
3 извиквания, приемливо. Съзнателно не мащабирано за голямо N: разход расте квадратично,
без измерена полза отвъд малкото N (виж план 15).

Сравненията вървят ПАРАЛЕЛНО (ThreadPoolExecutor), огледално на writer.write_candidates()
и verifier.check_all() в същия конвейер (виж research-plan-16-speed-ux-quality-roadmap.md —
установи, че v5 излиза по-бавен от v3 именно защото Съдията сериализираше независими
извиквания; research-plan-35-critique-of-plan-34.md, Критика 4, за точния патърн: всяко
сравнение връща чист tuple, `wins`/`pairwise_log` се пълнят СЛЕД като всички futures
приключат, в главната нишка — никога инкрементирани отвътре в паралелна задача, за да няма
race condition върху споделен dict).

Кандидат с нарушение на rules/forbidden.md (facts_check.CandidateVerification.blocked)
се изважда от сравнението изцяло — process-4.md: "не се смекчава". Ако ВСИЧКИ кандидати
са блокирани (краен, но възможен случай), последна линия вместо конвейерът да остане без
предложение — избира се кандидатът с най-малко непроследими твърдения, а причината влиза
в open_notes (огледално на v3's "подсигуряване", виж докстринга на runner.py).
"""

from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.brand.loader import BrandKnowledge
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import PairwiseVerdictSchema, PlanSchema
from src.pipeline.util import read_json, should_skip, write_json
from src.pipeline.verifier import CandidateVerification

JUDGEMENT_FILENAME = "judgement.json"

PAIRWISE_SYSTEM_PROMPT = """\
Ти си Съдията в конвейер за LinkedIn постове на ИКТ Клъстер – Варна. Получаваш ДВЕ версии
на един и същи пост и брой непроследими твърдения за всяка (вече проверено на код ниво —
не е твоя работа да преоткриваш тази проверка). Избираш КОЯ от двете е по-добра — НЕ
пишеш, НЕ редактираш, НЕ комбинираш.

Мини през четири части и реши за коя версия печели всяка: начало (първи ред), основен
абзац, акценти/списък, затваряне.

Критерии по важност:
1. По-малко непроследими твърдения печели автоматично — фактическа точност е над стил.
2. При равен брой непроследими твърдения — по-плътен, по-конкретен текст (собствени имена,
   числа от източниците) печели пред общи изречения.
3. Гласът/тонът срещу корпуса по-долу е последният критерий, не първият.

Контекст за глас и стил:

{brand_context}

Планът за този пост:
- тип: {post_type}
- тон: {tone}

Версия A ({name_a}) — непроследими твърдения: {unverified_a}
{text_a}

Версия B ({name_b}) — непроследими твърдения: {unverified_b}
{text_b}

Върни САМО JSON:
{{
  "winner": "A" | "B",
  "reasoning": {{"opening": "...", "body": "...", "highlights": "...", "closing": "..."}},
  "summary": "едно изречение"
}}
"""


@dataclass
class JudgementResult:
    """Резултатът от select() — записва се в judgement.json."""

    chosen_filename: str
    chosen_text: str
    wins: dict[str, int] = field(default_factory=dict)
    pairwise_results: list[dict[str, Any]] = field(default_factory=list)
    reasoning_summary: str = ""
    excluded_blocked: list[str] = field(default_factory=list)
    fallback_used: bool = False


def _compare_pair(
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    by_name: dict[str, str],
    verif_by_name: dict[str, CandidateVerification],
    name_a: str,
    name_b: str,
) -> tuple[str, str, str, str]:
    """
    Едно pairwise сравнение — извикано паралелно за всяка двойка от select() (виж докстринга
    на модула). Връща (name_a, name_b, winner_name, summary) — чист tuple, без да пипа
    споделено състояние (`wins`/`pairwise_log` живеят само в select(), попълват се СЛЕД като
    всички паралелни извиквания приключат).
    """
    prompt_system = PAIRWISE_SYSTEM_PROMPT.format(
        brand_context=knowledge.stable_context or "(няма зареден brand контекст)",
        post_type=plan.post_type,
        tone=plan.tone,
        name_a=name_a,
        unverified_a=verif_by_name[name_a].unverified_count,
        text_a=by_name[name_a],
        name_b=name_b,
        unverified_b=verif_by_name[name_b].unverified_count,
        text_b=by_name[name_b],
    )
    verdict = client.complete(
        Role.JUDGE,
        "Сравни версия A срещу версия B и избери една.",
        system=prompt_system,
        schema=PairwiseVerdictSchema,
    )
    assert isinstance(verdict, PairwiseVerdictSchema)
    winner_name = name_a if verdict.winner == "A" else name_b
    return name_a, name_b, winner_name, verdict.summary


def select(
    post_dir: Path,
    client: LLMClient,
    knowledge: BrandKnowledge,
    plan: PlanSchema,
    candidates: list[tuple[str, str]],
    verifications: list[CandidateVerification],
    *,
    force: bool = False,
) -> JudgementResult:
    judgement_path = post_dir / JUDGEMENT_FILENAME
    by_name = dict(candidates)

    if should_skip(judgement_path, force=force):
        data = read_json(judgement_path)
        if data.get("chosen_filename") in by_name:
            return JudgementResult(
                chosen_filename=data["chosen_filename"],
                chosen_text=by_name[data["chosen_filename"]],
                wins=data.get("wins", {}),
                pairwise_results=data.get("pairwise_results", []),
                reasoning_summary=data.get("reasoning_summary", ""),
                excluded_blocked=data.get("excluded_blocked", []),
                fallback_used=data.get("fallback_used", False),
            )
        # Записаният избор сочи към кандидат, който вече не съществува (напр. force е
        # променило набора) — преизчислява наново, вместо да гръмне.

    verif_by_name = {v.filename: v for v in verifications}
    eligible = [name for name, _ in candidates if not verif_by_name[name].blocked]
    excluded = [name for name, _ in candidates if verif_by_name[name].blocked]

    if not eligible:
        # Последна линия — виж докстринга на модула. Никога не оставяй човека без предложение.
        fallback = min(candidates, key=lambda pair: verif_by_name[pair[0]].unverified_count)[0]
        result = JudgementResult(
            chosen_filename=fallback,
            chosen_text=by_name[fallback],
            excluded_blocked=excluded,
            fallback_used=True,
            reasoning_summary=(
                "Всички кандидати нарушават rules/forbidden.md — избран е с най-малко "
                "непроследими твърдения; прегледайте ръчно преди публикуване."
            ),
        )
        _write(judgement_path, result)
        return result

    if len(eligible) == 1:
        # Един-единствен допустим кандидат — сравнение по двойки е безсмислено (0 двойки).
        chosen = eligible[0]
        result = JudgementResult(
            chosen_filename=chosen,
            chosen_text=by_name[chosen],
            wins={chosen: 0},
            excluded_blocked=excluded,
            reasoning_summary="Единствен кандидат без нарушение на rules/forbidden.md.",
        )
        _write(judgement_path, result)
        return result

    wins = {name: 0 for name in eligible}
    pairwise_log: list[dict[str, Any]] = []
    pairs = list(itertools.combinations(eligible, 2))
    with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
        futures = [
            executor.submit(_compare_pair, client, knowledge, plan, by_name, verif_by_name, name_a, name_b)
            for name_a, name_b in pairs
        ]
        # Събери резултатите тук, в главната нишка — futures могат да приключат в произволен
        # ред (виж докстринга на модула), но всеки резултат носи собствените си name_a/name_b,
        # затова редът на събиране няма значение за коректността.
        comparisons = [future.result() for future in futures]

    for name_a, name_b, winner_name, summary in comparisons:
        wins[winner_name] += 1
        pairwise_log.append({"a": name_a, "b": name_b, "winner": winner_name, "summary": summary})

    best_wins = max(wins.values())
    top = [name for name, count in wins.items() if count == best_wins]
    if len(top) > 1:
        # Тайбрек: най-малко непроследими твърдения, после азбучен ред на файла —
        # детерминирано, не второ LLM извикване само за да реши равенство.
        top.sort(key=lambda name: (verif_by_name[name].unverified_count, name))
    chosen = top[0]

    result = JudgementResult(
        chosen_filename=chosen,
        chosen_text=by_name[chosen],
        wins=wins,
        pairwise_results=pairwise_log,
        reasoning_summary=next((p["summary"] for p in pairwise_log if p["winner"] == chosen), ""),
        excluded_blocked=excluded,
    )
    _write(judgement_path, result)
    return result


def _write(path: Path, result: JudgementResult) -> None:
    write_json(
        path,
        {
            "chosen_filename": result.chosen_filename,
            "wins": result.wins,
            "pairwise_results": result.pairwise_results,
            "reasoning_summary": result.reasoning_summary,
            "excluded_blocked": result.excluded_blocked,
            "fallback_used": result.fallback_used,
        },
    )
