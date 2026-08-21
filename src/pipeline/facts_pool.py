"""
Стъпка [1.5] (v5) — Факт-регистър. Код, не LLM извикване — виж
docs/research-plans/research-plan-15-pipeline-speed-quality-rearchitecture.md, Част 4.

Защо съществува, при това след като Писателят вече получава facts.yaml/members.yaml
директно (план 11, Фаза 0, находка 2.6, от 2026-08-12): живо пускане на TUI на 2026-08-21
(план 15, Част 1) показа, че заземяването с "тук са фактите, които ЗНАЕМ" не пречи на
модела да измисли специфика, за която НИКОЙ източник не съществува — ранг на лектор, тема
на казуси, стая, линк за регистрация. Проблемът не е обърканост между известни факти
(тук помага), а увереност там, където няма никакъв източник.

facts_pool.json прави разликата явна и проверима на код ниво: не сурови YAML файлове,
подадени с надеждата моделът да ги разчете правилно, а ЕДИН плосък списък "позволени
конкретики за този пост" — точно него verifier.py (стъпка [4]) после сравнява буквално
срещу твърденията в кандидата, без LLM извикване за самата проверка.

Извличането на твърдения от брифа използва СЪЩИТЕ функции като src/quality/metrics.py
(find_proper_nouns/find_numbers) — една дефиниция на "какво е конкретно твърдение" в
цялата система, не втора регулярка, която може да се разсинхронизира с първата.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.brand.loader import BrandKnowledge
from src.pipeline.util import read_json, should_skip, write_json
from src.quality.metrics import find_numbers, find_proper_nouns

FACTS_POOL_FILENAME = "facts_pool.json"


@dataclass
class FactsPool:
    """Явен списък от факти, позволени за конкретния пост — резултатът от build()."""

    briefing_claims: list[str] = field(default_factory=list)
    facts_yaml: dict[str, Any] = field(default_factory=dict)
    mentionable_members: list[str] = field(default_factory=list)

    def allowed_strings(self) -> list[str]:
        """
        Плосък списък низове за буквална проверка на съвпадение (виж verifier.py) —
        включва суровия YAML dump на facts.yaml, за да покрие вложени стойности (адрес,
        ЕИК, имена на програми), не само върховите ключове на речника.
        """
        strings = list(self.briefing_claims) + list(self.mentionable_members)
        if self.facts_yaml:
            strings.append(yaml.safe_dump(self.facts_yaml, allow_unicode=True))
        return strings

    def to_dict(self) -> dict[str, Any]:
        return {
            "briefing_claims": self.briefing_claims,
            "facts_yaml": self.facts_yaml,
            "mentionable_members": self.mentionable_members,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FactsPool":
        return cls(
            briefing_claims=list(data.get("briefing_claims", [])),
            facts_yaml=data.get("facts_yaml") or {},
            mentionable_members=list(data.get("mentionable_members", [])),
        )


def build(post_dir: Path, briefing_text: str, knowledge: BrandKnowledge, *, force: bool = False) -> FactsPool:
    """Асемблира и записва facts_pool.json. Възобновяемост както навсякъде другаде в конвейера."""
    pool_path = post_dir / FACTS_POOL_FILENAME
    if should_skip(pool_path, force=force):
        return FactsPool.from_dict(read_json(pool_path))

    claims = sorted(set(find_proper_nouns(briefing_text) + find_numbers(briefing_text)))
    mentionable = [
        str(member.get("name", "")).strip()
        for member in knowledge.members
        if member.get("mention_allowed") is True and member.get("name")
    ]
    facts_yaml = knowledge.facts if (knowledge.facts_available and knowledge.facts) else {}

    pool = FactsPool(briefing_claims=claims, facts_yaml=facts_yaml, mentionable_members=mentionable)
    write_json(pool_path, pool.to_dict())
    return pool


def load(post_dir: Path) -> FactsPool | None:
    """Прочита вече записан регистър, или None ако липсва — за резюме/анализ извън run_pipeline."""
    pool_path = post_dir / FACTS_POOL_FILENAME
    if not pool_path.exists():
        return None
    return FactsPool.from_dict(read_json(pool_path))


def render_for_writer(pool: FactsPool) -> str:
    """
    Форматиран блок за системния промпт на Писателя (v5) — заменя render_sources_block
    (v3) с явния регистър вместо суровите YAML файлове. Съобщението е същото по дух:
    "с това имаш право да бъдеш конкретен", но сега е един готов списък, не два файла,
    за които моделът сам трябва да прецени релевантността.
    """
    claims_block = "\n".join(f"- {c}" for c in pool.briefing_claims) or (
        "(брифът не съдържа собствени имена или числа)"
    )
    if pool.facts_yaml:
        facts_yaml_text = yaml.safe_dump(pool.facts_yaml, allow_unicode=True, sort_keys=False)
        facts_block = f"```yaml\n{facts_yaml_text}```"
    else:
        facts_block = "(facts.yaml липсва или е празен)"
    members_block = "\n".join(f"- {m}" for m in pool.mentionable_members) or (
        "(няма член с потвърдено съгласие за споменаване)"
    )

    return (
        "## ФАКТ-РЕГИСТЪР — единственият източник за конкретика извън собствения ти стил\n\n"
        "Собствени имена и числа, извлечени от брифа:\n"
        f"{claims_block}\n\n"
        "identity/facts.yaml:\n"
        f"{facts_block}\n\n"
        "Членове с потвърдено съгласие за споменаване по име (mention_allowed: true):\n"
        f"{members_block}\n\n"
        "Правилото е едно: специфика (число, име, ранг, тема, детайл), която не е в никой "
        "от трите списъка по-горе, не съществува и не се пише — дори ако звучи правдоподобно "
        "за повода. Ако не знаеш нещо конкретно (напр. ранг на лектор, точна тема на "
        "програма), напиши изречението без тази специфика, вместо да я предположиш."
    )
