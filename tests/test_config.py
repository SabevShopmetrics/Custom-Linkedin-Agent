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
    assert config.request_timeout_seconds == 300  # подразбиране, не е зададено в .env (research-plan-36, А8)


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


# --------------------------------------------------------------- план 11, Фаза 1
# Празна стойност за параметър на извадката значи „не се изпраща" — не 0 и не грешка.
# Иначе `SEED=` в .env би сменил поведението на веригата, вместо да го остави каквото е.


def _set_required(monkeypatch):
    """Минимално валидна конфигурация през env променливи (те бият .env, override=False)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPENWEBUI_BASE_URL", "https://example.test")
    monkeypatch.setenv("OPENWEBUI_API_KEY", "key")
    for role in ("PLANNER", "WRITER", "MEDIA", "CRITIC", "CONSOLIDATOR", "FALLBACK"):
        monkeypatch.setenv(f"MODEL_{role}", f"{role.lower()}-model")


def test_empty_sampling_params_load_as_none(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SEED", "")
    monkeypatch.setenv("TEMPERATURE", "  ")
    monkeypatch.setenv("MAX_TOKENS", "")

    config = load_config()

    assert config.seed is None
    assert config.temperature is None
    assert config.max_tokens is None


def test_sampling_params_are_parsed_when_set(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("SEED", "42")
    monkeypatch.setenv("TEMPERATURE", "0.75")
    monkeypatch.setenv("MAX_TOKENS", "900")

    config = load_config()

    assert config.seed == 42
    assert config.temperature == 0.75
    assert config.max_tokens == 900


def test_non_numeric_temperature_is_a_clear_config_error(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("TEMPERATURE", "топло")

    with pytest.raises(ConfigError) as exc:
        load_config()

    assert "TEMPERATURE" in str(exc.value)


def test_child_environ_drops_values_loaded_from_dotenv_after_start(monkeypatch):
    # Уеб сървърът зарежда .env веднъж; без това всяка следваща задача наследяваше старите
    # стойности (на живо 2026-10-13: таймаут 120 s, въпреки REQUEST_TIMEOUT_SECONDS=300 в .env).
    import src.config as config_module

    monkeypatch.setattr(config_module, "_ENV_BEFORE_DOTENV", frozenset({"SHELL_ONLY"}))
    monkeypatch.setenv("SHELL_ONLY", "from-shell")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "120")  # като че ли заредено от .env по-късно

    env = config_module.child_environ()
    assert env["SHELL_ONLY"] == "from-shell"
    assert "REQUEST_TIMEOUT_SECONDS" not in env
