"""Споделени помощници за тестовете — без реални мрежови извиквания никъде."""

from __future__ import annotations

from typing import Any

from src.brand.loader import BrandKnowledge
from src.config import Config


def make_config(**overrides: Any) -> Config:
    base = dict(
        openwebui_base_url="https://example.test",
        openwebui_api_key="test-key",
        model_planner="planner-model",
        model_writer="writer-model",
        model_media="media-model",
        model_critic="critic-model",
        model_consolidator="consolidator-model",
        model_fallback="fallback-model",
        request_timeout_seconds=5,
        model_max_retries=3,
        invalid_response_max_retries=2,
        max_image_bytes=4 * 1024 * 1024,
    )
    base.update(overrides)
    return Config(**base)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def content_response(text: str) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": text}}]})


class StubClient:
    """LLMClient заместител за тестове на pipeline стъпки — гърми, ако някой го извика."""

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Моделът не трябваше да бъде извикван за тази стъпка (skip логиката е нарушена).")


def make_brand_knowledge(**overrides: Any) -> BrandKnowledge:
    base = dict(
        stable_context="",
        facts=None,
        facts_available=False,
        members=[],
        good_examples=[],
        bad_examples=[],
        audit=[],
    )
    base.update(overrides)
    return BrandKnowledge(**base)
