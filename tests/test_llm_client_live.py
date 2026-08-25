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

import pytest

from src.config import ConfigError, load_config
from src.llm.client import LLMClient
from src.llm.roles import Role
from src.llm.schema import PlanSchema

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
