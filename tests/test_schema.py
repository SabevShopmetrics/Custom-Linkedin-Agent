"""
Моделите не винаги връщат точния низ от промпта за FactCheck.source (напр. 'brief'
вместо 'briefing') — открито при реален end-to-end тест на 2026-08-10, където Критикът
изчерпа всичките си 3 поправящи опита заради това единствено разминаване. Нормализацията
трябва да поема известните варианти, вместо да гърми InvalidResponseError за отговор,
който семантично е напълно валиден.
"""

import pytest

from src.llm.schema import FactCheck, FactSource


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("brief", FactSource.BRIEFING),
        ("briefing", FactSource.BRIEFING),
        ("Briefing.md", FactSource.BRIEFING),
        ("facts", FactSource.FACTS_YAML),
        ("FACTS.YAML", FactSource.FACTS_YAML),
        ("members", FactSource.MEMBERS_YAML),
        ("Members.yaml", FactSource.MEMBERS_YAML),
        ("unverified", FactSource.UNVERIFIED),
        ("UNVERIFIED", FactSource.UNVERIFIED),
    ],
)
def test_fact_source_normalizes_known_aliases(raw_value, expected):
    fact = FactCheck(claim="твърдение", source=raw_value)
    assert fact.source == expected


def test_fact_source_rejects_unknown_value():
    with pytest.raises(Exception):
        FactCheck(claim="твърдение", source="откъде-си-я-измисли")
