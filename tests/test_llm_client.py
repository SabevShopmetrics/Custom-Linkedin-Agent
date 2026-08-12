"""
Retry с нарастващо изчакване → fallback модел → грешка; поправящ retry за невалиден
JSON → грешка. Никакви реални HTTP извиквания — requests.post е подменен изцяло.
"""

import requests

import src.llm.client as client_module
from src.errors import InvalidResponseError, ModelUnavailableError
from src.llm.client import LLMClient, _corrective_hint
from src.llm.roles import Role
from src.llm.schema import PlanSchema
from tests.conftest import content_response, make_config

import pytest

VALID_PLAN_JSON = (
    '{"post_type": "практики", "format": "текст+снимка", "tone": "топло", '
    '"needs_photos": true, "max_photo_count": 2, "is_sensitive": false, "notes": ""}'
)


def test_complete_success_on_first_try(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["model"])
        return content_response("Здравей, свят")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(), sleep_fn=lambda s: None)

    result = client.complete(Role.WRITER, "напиши нещо")

    assert result == "Здравей, свят"
    assert calls == ["writer-model"]
    assert client.call_log[0].success is True
    assert client.call_log[0].attempts == 1
    assert client.call_log[0].used_fallback is False


def test_complete_retries_on_transient_failure_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    def fake_post(url, headers, json, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise requests.exceptions.ConnectionError("временен проблем")
        return content_response("готово след retry")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    slept = []
    client = LLMClient(make_config(model_max_retries=3), sleep_fn=lambda s: slept.append(s))

    result = client.complete(Role.WRITER, "промпт")

    assert result == "готово след retry"
    assert attempts["n"] == 3
    assert len(slept) == 2  # изчакване след 1-ви и 2-ри неуспешен опит, нарастващо


def test_complete_falls_back_to_secondary_model_after_retries_exhausted(monkeypatch):
    def fake_post(url, headers, json, timeout):
        if json["model"] == "writer-model":
            raise requests.exceptions.ConnectionError("основният модел е недостъпен")
        return content_response("отговори fallback моделът")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(model_max_retries=2), sleep_fn=lambda s: None)

    result = client.complete(Role.WRITER, "промпт")

    assert result == "отговори fallback моделът"
    assert client.call_log[0].used_fallback is True


def test_complete_raises_model_unavailable_with_human_message_when_fallback_also_fails(monkeypatch):
    def fake_post(url, headers, json, timeout):
        raise requests.exceptions.ConnectionError("всичко е недостъпно")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(model_max_retries=2), sleep_fn=lambda s: None)

    with pytest.raises(ModelUnavailableError) as exc_info:
        client.complete(Role.WRITER, "промпт")

    assert "Open WebUI" in exc_info.value.human_message
    assert client.call_log[0].success is False


def test_complete_with_schema_parses_valid_json(monkeypatch):
    monkeypatch.setattr(
        client_module.requests, "post", lambda url, headers, json, timeout: content_response(VALID_PLAN_JSON)
    )
    client = LLMClient(make_config(), sleep_fn=lambda s: None)

    result = client.complete(Role.PLANNER, "анализирай брифа", schema=PlanSchema)

    assert isinstance(result, PlanSchema)
    assert result.post_type == "практики"
    assert result.needs_photos is True


def test_complete_with_schema_recovers_after_corrective_retry(monkeypatch):
    responses = iter(["това не е json изобщо", VALID_PLAN_JSON])

    def fake_post(url, headers, json, timeout):
        return content_response(next(responses))

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(invalid_response_max_retries=2), sleep_fn=lambda s: None)

    result = client.complete(Role.PLANNER, "анализирай брифа", schema=PlanSchema)

    assert isinstance(result, PlanSchema)


def test_complete_with_schema_corrective_message_includes_quote_hint_for_delimiter_errors(monkeypatch):
    """
    Регресионен тест за реален пропуск от 2026-08-10: моделът върна JSON с неескейпната
    ASCII кавичка вътре в стойност на низ ('Expecting ',' delimiter' от json.loads).
    Генеричното 'невалиден JSON' съобщение не сочи конкретния проблем — сега поправящият
    промпт трябва изрично да спомене кавичките, за да има модел шанс реално да го оправи.
    """
    broken_json = '{"post_type": "х", "format": "у "з"", "tone": "z", "needs_photos": false, "max_photo_count": 0, "is_sensitive": false}'
    captured_messages = []

    def fake_post(url, headers, json, timeout):
        captured_messages.append(json["messages"])
        if len(captured_messages) == 1:
            return content_response(broken_json)
        return content_response(
            '{"post_type": "х", "format": "y", "tone": "z", "needs_photos": false, "max_photo_count": 0, "is_sensitive": false}'
        )

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(invalid_response_max_retries=1), sleep_fn=lambda s: None)

    result = client.complete(Role.PLANNER, "анализирай брифа", schema=PlanSchema)

    assert isinstance(result, PlanSchema)
    second_call_messages = captured_messages[1]
    corrective_message = second_call_messages[-1]["content"]
    assert "кавичка" in corrective_message


def test_corrective_hint_flags_delimiter_errors_as_likely_quote_issue():
    assert "кавичка" in _corrective_hint("Expecting ',' delimiter: line 3 column 10 (char 42)")


def test_corrective_hint_empty_for_unrelated_errors():
    assert _corrective_hint("JSON не съответства на очакваната схема PlanSchema: ...") == ""


def test_complete_with_schema_gives_up_after_max_corrective_retries(monkeypatch):
    monkeypatch.setattr(
        client_module.requests,
        "post",
        lambda url, headers, json, timeout: content_response("никога не връщам валиден json"),
    )
    client = LLMClient(make_config(invalid_response_max_retries=1), sleep_fn=lambda s: None)

    with pytest.raises(InvalidResponseError) as exc_info:
        client.complete(Role.PLANNER, "анализирай брифа", schema=PlanSchema)

    assert "planner" in exc_info.value.human_message


def test_check_connection_reports_per_model_status_without_raising(monkeypatch):
    def fake_post(url, headers, json, timeout):
        if json["model"] == "media-model":
            raise requests.exceptions.ConnectionError("media моделът е зает")
        return content_response("ОК")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = LLMClient(make_config(), sleep_fn=lambda s: None)

    results = client.check_connection()

    assert results["planner"]["ok"] is True
    assert results["media"]["ok"] is False
    assert "media моделът е зает" in results["media"]["error"]


# --------------------------------------------------------------- план 11, Фаза 1
# Параметри на извадката. Съзнателното изискване: докато не са зададени, заявката е
# ТОЧНО каквато беше — сегашната верига има 20 реални пускания като евиденция и не бива
# да смени поведението си, защото е добавена настройка.


def test_request_payload_is_unchanged_when_no_sampling_params_are_set(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return content_response("текст")

    monkeypatch.setattr(requests, "post", fake_post)
    client = LLMClient(make_config())

    client.complete(Role.WRITER, "бриф")

    assert set(captured) == {"model", "messages"}


def test_seed_and_temperature_from_config_reach_the_request(monkeypatch):
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return content_response("текст")

    monkeypatch.setattr(requests, "post", fake_post)
    client = LLMClient(make_config(seed=42, temperature=0.7, max_tokens=800))

    client.complete(Role.WRITER, "бриф")

    assert captured["seed"] == 42
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 800


def test_per_call_temperature_overrides_the_configured_one(monkeypatch):
    """Нужно за кандидати с различна температура (process-4, стъпка 2)."""
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return content_response("текст")

    monkeypatch.setattr(requests, "post", fake_post)
    client = LLMClient(make_config(temperature=0.2))

    client.complete(Role.WRITER, "бриф", temperature=1.1)

    assert captured["temperature"] == 1.1
