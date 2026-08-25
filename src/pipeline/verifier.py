"""
Стъпка [4] (v5) — Филтър за проследимост. Заменя ФАКТИЧЕСКАТА част на Критика (v3) с
код-ниво проверка, без LLM извикване — виж
docs/research-plans/research-plan-15-pipeline-speed-quality-rearchitecture.md, Част 4.

Съзнателна редукция на обхвата спрямо пълен Chain-of-Verification (план 15, Част 2.3):
истинският CoVe отговаря на верификационни въпроси с LLM, независимо от черновата. Тук
въпросът "има ли източник за това твърдение" се отговаря ДЕТЕРМИНИРАНО — буквално
съвпадение срещу facts_pool.json/брифа — не с ново LLM извикване. Причината е изрична:
LLM, който отговаря на собствени верификационни въпроси, е същият клас риск (халюцинация),
който целият план опитва да намали, не различен механизъм. Покрива собствени имена, числа
и дати (regex, същите функции като src/quality/metrics.py) — най-честият клас измислена
специфика от живото пускане на 2026-08-21 (план 15, Част 1.2). Преформулировки, за които
буквалното съвпадение пропуска покритие, остават извън обхвата тук — маркирано изрично в
план 15, Недоказани твърдения, не подмятано като пълно решение.

GDPR проверките на код ниво (critic.enforce_code_level_rules) остават НЕПОКЪТНАТИ и се
пускат тук върху всеки кандидат — process-4.md, стъпка 4: "GDPR... се изпълнява непроменен,
върху всеки кандидат."

ЗАТВОРЕН ПРОПУСК (2026-08-25, беше "известен, съзнателен" в предишна версия на този
докстринг): `enforce_code_level_rules` хваща само GDPR (лично име извън брифа / организация
без mention_allowed) — НЕ нарушения на rules/forbidden.md (напр. "критикува конкретен
конкурент"), защото това е семантична преценка, не регекс. v5 нарочно не вика ПЪЛНИЯ LLM
Критик (факти+тон+правила накуп) върху отделните кандидати — целта на цялата верига, план
15 — затова forbidden.md нарушение не можеше да се хване преди Съдията да избере, само
асинхронно СЛЕД избора (стъпка [7], информативно, твърде късно за реален pre-publish риск).

Затворено с `critic.check_forbidden_rules()` — умишлено МНОГО по-лека LLM проверка от пълния
Критик (само секцията ЗАБРАНЕНО, без факти/тон/GDPR), викана паралелно за всеки кандидат в
`check_all()` по-долу. Не връща пълния v3 разход (план 15 се опитваше точно да го премахне),
но затваря реалната дупка: `CandidateVerification.blocked` вече РЕАЛНО получава "forbidden"
находки, не само теоретично поддържа полето.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.brand.loader import BrandKnowledge
from src.llm.client import LLMClient
from src.llm.schema import CritiqueSchema, RuleViolation, Verdict
from src.pipeline import critic
from src.pipeline.facts_pool import FactsPool
from src.pipeline.util import read_json, should_skip, write_json
from src.quality.metrics import find_numbers, find_proper_nouns

FACTS_CHECK_FILENAME = "facts_check.json"


@dataclass
class ClaimVerification:
    """Едно твърдение (собствено име/число) от кандидата + дали е покрито от факт-регистъра."""

    claim: str
    verified: bool


@dataclass
class CandidateVerification:
    """Резултатът от проверката на ЕДИН кандидат — влиза в facts_check.json и в Съдията."""

    filename: str
    claims: list[ClaimVerification] = field(default_factory=list)
    rule_violations: list[RuleViolation] = field(default_factory=list)

    @property
    def unverified_count(self) -> int:
        return sum(1 for c in self.claims if not c.verified)

    @property
    def blocked(self) -> bool:
        """
        Изключен от избора на Съдията. Огледално на critic.py: нарушение извън GDPR
        ("forbidden") е дисквалифициращо; GDPR сам по себе си не блокира кандидата тук
        (влиза само като открита бележка, ако кандидатът все пак бъде избран).
        """
        return any(v.rule != "gdpr" for v in self.rule_violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "claims": [{"claim": c.claim, "verified": c.verified} for c in self.claims],
            "rule_violations": [{"rule": v.rule, "detail": v.detail} for v in self.rule_violations],
            "unverified_count": self.unverified_count,
            "blocked": self.blocked,
        }


def _claim_is_covered(claim: str, allowed: list[str]) -> bool:
    """Буквално съвпадение — твърдението е подниза на някой от позволените низове."""
    return any(claim in source for source in allowed)


def check_candidate(
    filename: str,
    text: str,
    briefing_text: str,
    pool: FactsPool,
    knowledge: BrandKnowledge,
    client: LLMClient,
) -> CandidateVerification:
    """
    Проверява ЕДИН кандидат: твърдения срещу facts_pool (код) + GDPR (код) +
    rules/forbidden.md (лека LLM проверка — виж critic.check_forbidden_rules и докстринга
    на модула за защо това последното е единственото извикване към модел тук).
    """
    allowed = pool.allowed_strings() + [briefing_text]
    raw_claims = sorted(set(find_proper_nouns(text) + find_numbers(text)))
    claims = [ClaimVerification(claim=claim, verified=_claim_is_covered(claim, allowed)) for claim in raw_claims]

    # Преизползва СЪЩАТА функция като v3's Критик, непроменена — виж докстринга на модула.
    throwaway = CritiqueSchema(verdict=Verdict.OK)
    critic.enforce_code_level_rules(throwaway, text, briefing_text, knowledge.members)

    violations = list(throwaway.rule_violations)
    violations.extend(critic.check_forbidden_rules(client, knowledge, text))

    return CandidateVerification(filename=filename, claims=claims, rule_violations=violations)


def check_all(
    post_dir: Path,
    candidates: list[tuple[str, str]],
    briefing_text: str,
    pool: FactsPool,
    knowledge: BrandKnowledge,
    client: LLMClient,
    *,
    force: bool = False,
) -> list[CandidateVerification]:
    """
    Проверява всички кандидати и записва facts_check.json. Възобновяемост както навсякъде.

    Единственото LLM извикване тук (check_forbidden_rules) тече ПАРАЛЕЛНО за всички
    кандидати — огледално на writer.write_candidates — за да не сериализира N леки
    извиквания едно след друго и да изяде обратно спестеното от план 15 време.
    """
    check_path = post_dir / FACTS_CHECK_FILENAME
    if should_skip(check_path, force=force):
        data = read_json(check_path)
        by_name = dict(candidates)
        results = []
        for entry in data.get("candidates", []):
            results.append(
                CandidateVerification(
                    filename=entry["filename"],
                    claims=[ClaimVerification(**c) for c in entry.get("claims", [])],
                    rule_violations=[RuleViolation(**v) for v in entry.get("rule_violations", [])],
                )
            )
        if all(r.filename in by_name for r in results) and len(results) == len(candidates):
            return results
        # Записаният файл не съвпада с текущия набор кандидати (напр. N се е сменило) —
        # преизчислява наново, вместо да върне непълен/несъответстващ резултат.

    if not candidates:
        results: list[CandidateVerification] = []
    else:
        with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
            futures = [
                executor.submit(check_candidate, filename, text, briefing_text, pool, knowledge, client)
                for filename, text in candidates
            ]
            results = [future.result() for future in futures]
    write_json(check_path, {"candidates": [r.to_dict() for r in results]})
    return results
