"""
Чете .env и излага типизирана конфигурация.

Мапингът роля → модел живее тук (взет от .env), не се хардкодва в pipeline логиката —
виж src/llm/roles.py за самото съпоставяне.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Липсваща или невалидна конфигурация в .env."""


@dataclass(frozen=True)
class Config:
    openwebui_base_url: str
    openwebui_api_key: str

    model_planner: str
    model_writer: str
    model_media: str
    model_critic: str
    model_consolidator: str
    model_fallback: str

    request_timeout_seconds: int = 120
    model_max_retries: int = 3
    invalid_response_max_retries: int = 2

    # лимит за размер на снимка, изпращана към vision модел, преди ImageTooLargeError
    max_image_bytes: int = 4 * 1024 * 1024


_REQUIRED_VARS = (
    "OPENWEBUI_BASE_URL",
    "OPENWEBUI_API_KEY",
    "MODEL_PLANNER",
    "MODEL_WRITER",
    "MODEL_MEDIA",
    "MODEL_CRITIC",
    "MODEL_CONSOLIDATOR",
    "MODEL_FALLBACK",
)


def load_config(env_path: Path | None = None) -> Config:
    """
    Зарежда конфигурацията от .env (или от вече заредени env променливи, ако .env липсва —
    полезно за тестове и CI).

    Хвърля ConfigError с ясен списък кои променливи липсват, вместо неясен KeyError.
    """
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Липсват задължителни настройки в .env: "
            + ", ".join(missing)
            + ". Копирайте .env.example на .env и попълнете стойностите."
        )

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"'{name}' в .env трябва да е цяло число, получено: '{raw}'") from exc

    return Config(
        openwebui_base_url=os.environ["OPENWEBUI_BASE_URL"].rstrip("/"),
        openwebui_api_key=os.environ["OPENWEBUI_API_KEY"],
        model_planner=os.environ["MODEL_PLANNER"],
        model_writer=os.environ["MODEL_WRITER"],
        model_media=os.environ["MODEL_MEDIA"],
        model_critic=os.environ["MODEL_CRITIC"],
        model_consolidator=os.environ["MODEL_CONSOLIDATOR"],
        model_fallback=os.environ["MODEL_FALLBACK"],
        request_timeout_seconds=_int_env("REQUEST_TIMEOUT_SECONDS", 120),
        model_max_retries=_int_env("MODEL_MAX_RETRIES", 3),
        invalid_response_max_retries=_int_env("INVALID_RESPONSE_MAX_RETRIES", 2),
    )
