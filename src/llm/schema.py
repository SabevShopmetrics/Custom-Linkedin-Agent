"""
Схеми за структурирания изход на всяка роля + валидиране веднага след получаване
от модела. Модел, който върне текст вместо JSON, трябва да гръмне тук — не три
стъпки по-късно в конвейера.
"""

from __future__ import annotations

import json
from enum import Enum

from pydantic import BaseModel, Field, ValidationError, field_validator


class PlanSchema(BaseModel):
    """Изход на Планера — записва се в plan.json."""

    post_type: str = Field(description="напр. 'практики', 'събитие', 'партньорство', 'общо'")
    format: str = Field(description="напр. 'текст+снимка', 'само текст', 'карусел'")
    tone: str = Field(description="кратко описание на желания тон за този конкретен пост")
    needs_photos: bool
    max_photo_count: int = Field(ge=0, le=10)
    is_sensitive: bool = Field(
        description="true ако темата изисква човешко одобрение преди публикуване"
    )
    notes: str = Field(default="", description="кратки бележки за Писателя и Media Picker")


class MediaSelectionItem(BaseModel):
    filename: str
    reason: str
    possible_minor: bool = Field(
        default=False,
        description="true ако снимката вероятно показва лице под 18 г. — задейства задължителен човешки преглед",
    )


class MediaSelectionSchema(BaseModel):
    """Изход на Media Picker — записва се в media.json."""

    selected: list[MediaSelectionItem] = Field(default_factory=list)


class Verdict(str, Enum):
    OK = "ok"
    NEEDS_REVISION = "needs_revision"
    HARD_REJECT = "hard_reject"


class FactSource(str, Enum):
    BRIEFING = "briefing"
    FACTS_YAML = "facts.yaml"
    MEMBERS_YAML = "members.yaml"
    UNVERIFIED = "UNVERIFIED"


_SOURCE_ALIASES = {
    "brief": FactSource.BRIEFING,
    "briefing": FactSource.BRIEFING,
    "briefing.md": FactSource.BRIEFING,
    "бриф": FactSource.BRIEFING,
    "facts": FactSource.FACTS_YAML,
    "facts.yaml": FactSource.FACTS_YAML,
    "facts.yml": FactSource.FACTS_YAML,
    "members": FactSource.MEMBERS_YAML,
    "members.yaml": FactSource.MEMBERS_YAML,
    "members.yml": FactSource.MEMBERS_YAML,
    "unverified": FactSource.UNVERIFIED,
    "unknown": FactSource.UNVERIFIED,
    "none": FactSource.UNVERIFIED,
}


class FactCheck(BaseModel):
    """Едно фактическо твърдение от чернова + до кой източник е проследено."""

    claim: str
    source: FactSource
    note: str = ""

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, value: object) -> object:
        """
        Моделите не винаги връщат точния низ от промпта (напр. 'brief' вместо
        'briefing') — нормализираме известни варианти вместо да хвърляме
        InvalidResponseError и да прахосваме поправящ retry за семантично ясен отговор.
        """
        if isinstance(value, str):
            normalized = _SOURCE_ALIASES.get(value.strip().lower())
            if normalized is not None:
                return normalized
        return value


class RuleViolation(BaseModel):
    """
    Нарушение на rules/forbidden.md или rules/gdpr.md. Не спира конвейера (от 2026-08-11) —
    влиза в дневния ред на втория пост като находка, която задължително трябва да отпадне,
    и остава видимо в critique.md / meta.json.
    """

    rule: str = Field(description="'gdpr' или 'forbidden'")
    detail: str


class CritiqueSchema(BaseModel):
    """
    Изход на Критика — вгражда се в critique.md като JSON блок.

    Три отделни проверки, всяка срещу конкретен източник (виж
    docs/prompts/followup-prompt-2-knowledge-critic.md, раздел 4):
      - facts        → сверени срещу briefing/facts.yaml/members.yaml
      - tone_issues   → сверени срещу voice/tone.md и voice/vocabulary.md
      - rule_violations → сверени срещу rules/forbidden.md и rules/gdpr.md

    Нарушение извън GDPR вдига verdict до HARD_REJECT (виж
    src/pipeline/critic.py:enforce_code_level_rules), дори ако моделът сам не го е сложил.
    От 2026-08-11 verdict е само етикет за сериозност — не спира конвейера и не отхвърля
    поста; сортира находките в дневния ред на втория пост (виж critic.collect_findings).
    """

    verdict: Verdict
    facts: list[FactCheck] = Field(default_factory=list)
    tone_issues: list[str] = Field(default_factory=list)
    rule_violations: list[RuleViolation] = Field(default_factory=list)
    summary: str = Field(default="", description="едно изречение обобщение на преценката")


def parse_structured(raw_text: str, schema: type[BaseModel]) -> BaseModel:
    """
    Опитва да извлече и валидира JSON от суров текстов отговор на модел.

    Толерантно е към модели, които обграждат JSON-а с ```json ... ``` или с обяснителен
    текст около него — търси първата балансирана { ... } подпоследователност.

    При провал хвърля ValueError с описателно съобщение — извикващият код (llm/client.py)
    превръща това в InvalidResponseError с broй опити.
    """
    candidate = _extract_json_object(raw_text)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Отговорът не е валиден JSON: {exc}") from exc

    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"JSON не съответства на очакваната схема {schema.__name__}: {exc}") from exc


def _extract_json_object(text: str) -> str:
    text = text.strip()
    # премахни markdown fence, ако моделът е обвил отговора в ```json ... ```
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[len("json") :].strip()
            if part.startswith("{"):
                text = part
                break

    start = text.find("{")
    if start == -1:
        raise ValueError("В отговора няма '{' — моделът вероятно не е върнал JSON.")

    depth = 0
    for idx in range(start, len(text)):
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    raise ValueError("Незатворена JSON структура в отговора на модела.")
