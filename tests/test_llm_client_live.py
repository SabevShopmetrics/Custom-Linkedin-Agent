"""
Единственият тест, който наистина вика конфигурирания в .env модел — маркиран `live`,
изключен от подразбиращото се пускане (виж pyproject.toml, `addopts = "-m 'not live'"`).

Защо съществува: нито един от другите ~250 теста не пипа реалната мрежа (виж докстринга на
test_llm_client.py — `requests.post` е изцяло подменен навсякъде другаде). Затова нищо не
хвана JSON-retry бурята и 345-секундния Критик разговор от реално пускане (2026-08-25,
posts/2026-08-25-organizirame-godishnata-konferentsiya-digi-2/output/meta.json) — целият
разход дойде от втори опит на структуриран отговор, невидим за никой fake-client тест.

Целта тук НЕ е да замести fake-client тестовете (те остават правилното мнозинство — бързи,
детерминирани, без разход) — а да даде поне ЕДНА проверка, че structured-output round-trip-ът
реално работи срещу конфигурирания бекенд, преди да го научим от живо, скъпо пускане.

Пускане изрично: `pytest -m live`. Пропуска се тихо, ако .env липсва/е непълен — тестът не е
предназначен за CI или машина без реални credentials.
"""

from __future__ import annotations

import time

import pytest
import requests

from src.config import ConfigError, load_config
from src.llm.client import LLMClient
from src.llm.roles import Role, model_for_role
from src.llm.schema import PairwiseVerdictSchema, PlanSchema

pytestmark = pytest.mark.live


def _live_client() -> LLMClient:
    try:
        config = load_config()
    except ConfigError:
        pytest.skip(".env не е конфигуриран (или е непълен) — живият тест се пропуска.")
    return LLMClient(config)


def test_planner_role_returns_valid_structured_output():
    """
    Възпроизвежда конкретния клас проблем от реалното пускане на 2026-08-25: структуриран
    изход (schema=PlanSchema) от конфигурирания Планер модел върху реалистичен бриф — не
    просто "отговори с ОК" (виж test_check_connection_reports_every_configured_role за това
    по-леко ниво). Ако моделът редовно се нуждае от повече от 1 поправящ опит тук, това е
    сигнал, забелязан от теста, не открит на парче от живо пускане.
    """
    client = _live_client()
    brief = "Утре имаме среща на клъстера с 15 души в офиса на бул. Ген. Колев 54, Варна."
    system = (
        "Ти си Планер в конвейер за LinkedIn постове. Върни САМО валиден JSON по схемата: "
        '{"post_type": "...", "format": "...", "tone": "...", "needs_photos": true|false, '
        '"max_photo_count": 0, "is_sensitive": true|false, "notes": "..."}'
    )

    result = client.complete(Role.PLANNER, brief, system=system, schema=PlanSchema)

    assert isinstance(result, PlanSchema)
    assert result.post_type


def test_check_connection_reports_every_configured_role():
    """По-лека проверка — всеки конфигуриран модел отговаря на минимален промпт (виж `cli check`)."""
    client = _live_client()

    results = client.check_connection()

    failed = {label: info for label, info in results.items() if not info["ok"]}
    assert not failed, f"Неотговорили модели: {failed}"


def test_response_format_json_schema_support_and_latency():
    """
    Инвестигативен спайк, не production проверка — research-plan-34-speed-quality-second-
    pass.md (Част 2) / research-plan-35-critique-of-plan-34.md (Критика 3): преди да се
    добавя `response_format`/`guided_json` в `LLMClient` зад флаг, трябва РЕАЛНО измерване
    върху конфигурирания бекенд (`chat.velocity.online` към момента на писане), не цитат от
    външна литература — "near-zero overhead" твърдения там са едностранчиви (виж критиката,
    "The Hidden Cost of Structured Generation in LLMs" стига до обратен извод в друг контекст).

    Не пипа `LLMClient._post_chat` (той нарочно не поддържа `response_format` още) — вика
    суровия endpoint директно с `requests.post`, огледално на `_post_chat`, само с добавен
    `response_format` параметър, за да измери:
      1. Поддържа ли бекендът параметъра изобщо (приема заявката, връща валиден JSON от
         първия опит) — важи като печатан резултат за човек, не assert (гърми, ако не се
         поддържа, е самата информация, не провал на теста).
      2. Латентност С параметъра срещу латентност БЕЗ него (`client.complete()`, съществуващият
         path), за да се прецени нетния ефект преди Ред 3б от План 34.

    Резултатите се печатат (`pytest -m live -s -k response_format`) за ръчен преглед — тестът
    само проверява, че НИТО ЕДИН от двата пътя не гърми неочаквано (мрежова грешка е реален
    провал; "непознат параметър, игнориран от сървъра" не е — точно затова не е assert върху
    съдържанието на отговора отвъд валиден JSON).
    """
    client = _live_client()
    config = client.config
    model = model_for_role(Role.JUDGE, config)

    system = (
        "Ти си Съдия. Сравни версия A срещу версия B и избери по-добрата. Върни САМО JSON: "
        '{"winner": "A"|"B", "reasoning": {"opening": "...", "body": "...", '
        '"highlights": "...", "closing": "..."}, "summary": "едно изречение"}'
    )
    prompt = (
        "Версия A: Клъстерът проведе среща на 5 август с 15 души.\n"
        "Версия B: Имахме страхотна среща наскоро, беше супер полезно."
    )

    # --- 1. Базова линия: съществуващият path, БЕЗ response_format ---
    started = time.monotonic()
    baseline = client.complete(Role.JUDGE, prompt, system=system, schema=PairwiseVerdictSchema)
    baseline_seconds = time.monotonic() - started
    assert isinstance(baseline, PairwiseVerdictSchema)
    baseline_attempts = client.call_log[-1].attempts

    # --- 2. Същата заявка, РЪЧНО с response_format (JSON Schema на PairwiseVerdictSchema) ---
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "pairwise_verdict",
                "schema": PairwiseVerdictSchema.model_json_schema(),
            },
        },
    }
    started = time.monotonic()
    try:
        response = requests.post(
            f"{config.openwebui_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.openwebui_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=config.request_timeout_seconds,
        )
        structured_seconds = time.monotonic() - started
        structured_supported = response.ok
        structured_detail = response.text[:300] if not response.ok else "(200 OK)"
    except requests.RequestException as exc:
        structured_seconds = time.monotonic() - started
        structured_supported = False
        structured_detail = f"{type(exc).__name__}: {exc}"

    # Латинска транслитерация нарочно, не кирилица, и без non-ASCII пунктуация — Windows
    # конзолата тук е cp1252 по подразбиране (не UTF-8, за разлика от cli.py's
    # force_utf8_io(), който не се прилага в тестов контекст), а `structured_detail` може да
    # носи кирилица от самия сървърен отговор при грешка — печатането е диагностика, никога
    # не бива само то да съсипе теста, ако `sys.stdout` не поддържа някой байт от нея.
    message = (
        "\n[response_format spike] "
        f"baseline: {baseline_seconds:.1f}s, {baseline_attempts} attempt(s) | "
        f"with response_format: {structured_seconds:.1f}s, supported={structured_supported} "
        f"({structured_detail})"
    )
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", errors="backslashreplace").decode("ascii"))
