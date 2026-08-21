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
    """Типизирана конфигурация от .env — резултатът от load_config(), подаван на LLMClient."""

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

    # Параметри на извадката. `None` значи „не се изпраща в заявката" — тоест поведението
    # по подразбиране е точно както преди въвеждането им (план 11, Фаза 1: находка 2.7).
    # Нарочно е така: сегашната верига има 20 реални пускания като евиденция и не бива да
    # смени поведението си, защото е добавена настройка.
    #
    # SEED е основната причина полетата да съществуват: без него две пускания със същия
    # бриф дават различен текст и всяко сравнение „преди/след" е несигурно. TEMPERATURE
    # става нужна във Фаза 3 (кандидати с различна температура).
    temperature: float | None = None
    seed: int | None = None
    max_tokens: int | None = None

    # лимит за размер на снимка, изпращана към vision модел, преди ImageTooLargeError
    max_image_bytes: int = 4 * 1024 * 1024

    # --- v5 (research-plan-15-pipeline-speed-quality-rearchitecture.md, надгражда
    # docs/technical/process-4.md) — изборът на верига е конфигурация, не код (process-4.md,
    # Част 0): "v3" (подразбиране) не пипа нищо от сегашния конвейер; "v5" избира новата
    # верига в runner.py._run_pipeline_v5. Всяка друга стойност пада обратно на "v3".
    pipeline_chain: str = "v3"

    # Брой паралелни кандидата в стъпка [2] на v5 (process-4.md, "WRITER_CANDIDATES,
    # подразбиране 3"). Без значение при pipeline_chain="v3".
    writer_candidates: int = 3

    # Модел за ролята Съдия (v5, стъпка [5]). Празно → model_for_role презема model_critic
    # (виж src/llm/roles.py) — нарочно НЕ е добавено в _REQUIRED_VARS, за да не се изисква
    # редактиране на .env само за да продължи да работи v3, докато v5 е изключен.
    model_judge: str = ""


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
    # override=False означава: ако променливата вече е в os.environ (напр. зададена от
    # тестове или от shell), .env НЕ я презаписва.
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    # Провери всички задължителни полета накуп и хвърли ЕДНА грешка със списъка от
    # липсващи — вместо да гръмне на първото и човек да поправя .env на парчета.
    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise ConfigError(
            "Липсват задължителни настройки в .env: "
            + ", ".join(missing)
            + ". Копирайте .env.example на .env и попълнете стойностите."
        )

    # --- малки помощници за парсване на числови настройки, дефинирани тук (не на модулно
    # ниво), защото им трябва достъп до ConfigError с конкретното име на променливата

    def _int_env(name: str, default: int) -> int:
        """Задължително цяло число с подразбиране, ако липсва; ConfigError при грешен формат."""
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"'{name}' в .env трябва да е цяло число, получено: '{raw}'") from exc

    def _optional_int_env(name: str) -> int | None:
        """Празно/липсващо → None (виж коментара при Config.temperature защо е важно)."""
        raw = os.environ.get(name)
        if not raw or not raw.strip():
            return None
        return _int_env(name, 0)

    def _optional_float_env(name: str) -> float | None:
        """Същото като _optional_int_env, но за число с плаваща запетая (TEMPERATURE)."""
        raw = os.environ.get(name)
        if not raw or not raw.strip():
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"'{name}' в .env трябва да е число, получено: '{raw}'") from exc

    # Сглобява финалния обект — базовият URL се почиства от завършващо "/", за да не се
    # получат двойни наклонени черти при конкатенация в llm/client.py.
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
        temperature=_optional_float_env("TEMPERATURE"),
        seed=_optional_int_env("SEED"),
        max_tokens=_optional_int_env("MAX_TOKENS"),
        pipeline_chain=(os.environ.get("PIPELINE_CHAIN", "v3").strip().lower() or "v3"),
        writer_candidates=_int_env("WRITER_CANDIDATES", 3),
        model_judge=os.environ.get("MODEL_JUDGE", "").strip(),
    )
