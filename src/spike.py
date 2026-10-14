"""
`python -m src.cli spike` — измерване на живо, преди да се включат опционалните ускорявания
(research-plan-36, Б3/Б4/Б5; наследено от спайка в research-plan-34/35, който никога не беше
пуснат).

Пуска ЕДНА и съща истинска заявка на Планера (същия системен промпт с корпуса, примерен бриф
от bench/briefs/) в четири варианта и за всеки отпечатва време, дали сървърът е приел заявката
и дали отговорът е валиден JSON по схемата:

  1. както днес (без нищо)
  2. без „мислене"            — chat_template_kwargs.enable_thinking=false
  3. гарантиран JSON           — response_format с JSON схемата
  4. и двете

Не пипа нищо в posts/ и не сменя .env — само отпечатва препоръка кои редове да се сложат.
Всеки вариант се пуска `--repeat` пъти (подразбиране 2), защото времето на един и същ модел
варира силно според натоварването на сървъра (план 34: 28–95s за сходна работа).
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from src.brand.loader import load_brand_knowledge
from src.config import Config
from src.llm.client import LLMClient
from src.llm.roles import Role, model_for_role
from src.llm.schema import PlanSchema, parse_structured
from src.pipeline.planner import SYSTEM_PROMPT_TEMPLATE

DEFAULT_BRIEF = Path("bench/briefs/02-bogat-brief-sabitie.md")

_VARIANTS = [
    ("както днес", {}),
    ("без мислене", {"chat_template_kwargs": {"enable_thinking": False}}),
    (
        "гарантиран JSON",
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "PlanSchema", "schema": PlanSchema.model_json_schema()},
            }
        },
    ),
]
_VARIANTS.append(("и двете", {**_VARIANTS[1][1], **_VARIANTS[2][1]}))


def _one_call(client: LLMClient, model: str, messages: list[dict], extra: dict) -> tuple[float, str, bool]:
    """(секунди, грешка или "", валиден JSON ли е)."""
    started = time.monotonic()
    try:
        text = client._post_chat(model, messages, extra=extra)  # noqa: SLF001 — нарочно без retry
    except Exception as exc:  # диагностична команда — всяка грешка е резултат, не срив
        return time.monotonic() - started, f"{type(exc).__name__}: {str(exc)[:120]}", False
    elapsed = time.monotonic() - started
    try:
        parse_structured(text, PlanSchema)
        return elapsed, "", True
    except ValueError:
        return elapsed, "", False


def run_spike(config: Config, *, brief_path: Path = DEFAULT_BRIEF, repeat: int = 2, out=print) -> int:
    knowledge = load_brand_knowledge()
    brief = brief_path.read_text(encoding="utf-8") if brief_path.exists() else "Проведохме среща с 15 фирми членове."
    system = SYSTEM_PROMPT_TEMPLATE.format(brand_context=knowledge.stable_context or "(няма)")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{brief}\n\n---\nПапката photos/ е празна — няма снимки."},
    ]
    client = LLMClient(config)
    model = model_for_role(Role.PLANNER, config)
    out(f"Модел на Планера: {model} · бриф: {brief_path} · {repeat} пъти на вариант\n")

    rows = []
    for label, extra in _VARIANTS:
        times, errors, valid = [], [], 0
        for _ in range(max(1, repeat)):
            seconds, error, ok_json = _one_call(client, model, messages, extra)
            if error:
                errors.append(error)
            else:
                times.append(seconds)
                valid += int(ok_json)
        median = statistics.median(times) if times else None
        rows.append((label, median, valid, len(times), errors))
        shown = f"{median:6.1f} сек." if median is not None else "   —    "
        status = f"валиден JSON {valid}/{len(times)}" if times else f"ГРЕШКА: {errors[0]}"
        out(f"  {label:18} {shown}   {status}")

    baseline = rows[0][1]
    out("")
    if baseline is None:
        out("Основната заявка не мина — проверете връзката (/check) преди да мерите ускорявания.")
        return 1
    suggestions = []
    no_think, json_mode = rows[1], rows[2]
    if no_think[1] is not None and no_think[2] == no_think[3] and no_think[1] < baseline * 0.8:
        suggestions.append(
            f"NO_THINKING_ROLES=planner  (≈{baseline - no_think[1]:.0f} сек. по-бързо на Планера; за critic/judge "
            f"— само след сравнение на качеството с `bench`, виж research-plan-36, Б5)"
        )
    if json_mode[1] is not None and json_mode[2] == json_mode[3] and json_mode[1] <= baseline * 1.2:
        suggestions.append("STRUCTURED_OUTPUT=json_schema  (сървърът го поддържа и не забавя)")
    if suggestions:
        out("Препоръка за .env:")
        for line in suggestions:
            out(f"  {line}")
    else:
        out("Нито едно ускорение не показа ясна полза на този сървър — оставете .env непроменен.")
    return 0
