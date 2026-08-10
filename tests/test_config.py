"""Липсваща конфигурация трябва да дава ясен списък какво липсва, не KeyError."""

import pytest

from src.config import ConfigError, load_config


def _clear_env(monkeypatch):
    for name in [
        "OPENWEBUI_BASE_URL",
        "OPENWEBUI_API_KEY",
        "MODEL_PLANNER",
        "MODEL_WRITER",
        "MODEL_MEDIA",
        "MODEL_CRITIC",
        "MODEL_CONSOLIDATOR",
        "MODEL_FALLBACK",
        "REQUEST_TIMEOUT_SECONDS",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_missing_required_vars_raise_config_error_with_names(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    empty_env = tmp_path / ".env"
    empty_env.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_config(env_path=empty_env)

    message = str(exc_info.value)
    assert "OPENWEBUI_API_KEY" in message
    assert "MODEL_WRITER" in message
    assert ".env.example" in message


def test_complete_config_loads_successfully(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENWEBUI_BASE_URL=https://chat.velocity.online",
                "OPENWEBUI_API_KEY=abc123",
                "MODEL_PLANNER=planner-model",
                "MODEL_WRITER=writer-model",
                "MODEL_MEDIA=media-model",
                "MODEL_CRITIC=critic-model",
                "MODEL_CONSOLIDATOR=consolidator-model",
                "MODEL_FALLBACK=fallback-model",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(env_path=env_file)

    assert config.openwebui_base_url == "https://chat.velocity.online"
    assert config.model_writer == "writer-model"
    assert config.request_timeout_seconds == 120  # подразбиране, не е зададено в .env


def test_non_numeric_timeout_raises_clear_error(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENWEBUI_BASE_URL=https://x",
                "OPENWEBUI_API_KEY=k",
                "MODEL_PLANNER=p",
                "MODEL_WRITER=w",
                "MODEL_MEDIA=m",
                "MODEL_CRITIC=c",
                "MODEL_CONSOLIDATOR=co",
                "MODEL_FALLBACK=f",
                "REQUEST_TIMEOUT_SECONDS=не-число",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="REQUEST_TIMEOUT_SECONDS"):
        load_config(env_path=env_file)
