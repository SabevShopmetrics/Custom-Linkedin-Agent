"""
Роля → модел. Логиката на конвейера иска модел по роля (Role enum), никога по име —
съпоставянето живее изцяло в конфигурацията (.env), тук е само мапингът към полето
в Config, за да може да се смени модел без да се пипа pipeline код.
"""

from __future__ import annotations

from enum import Enum

from src.config import Config


class Role(str, Enum):
    """Логическа роля в конвейера — не име на модел. Виж _ROLE_TO_CONFIG_FIELD за съпоставянето."""

    PLANNER = "planner"
    WRITER = "writer"
    MEDIA = "media"
    CRITIC = "critic"
    CONSOLIDATOR = "consolidator"
    # v5 (research-plan-15, надгражда process-4.md) — Съдия: сравнява кандидати по двойки,
    # не пише текст. Без значение, докато pipeline_chain="v3" (виж src/pipeline/runner.py).
    JUDGE = "judge"


_ROLE_TO_CONFIG_FIELD: dict[Role, str] = {
    Role.PLANNER: "model_planner",
    Role.WRITER: "model_writer",
    Role.MEDIA: "model_media",
    Role.CRITIC: "model_critic",
    Role.CONSOLIDATOR: "model_consolidator",
    Role.JUDGE: "model_judge",
}


def model_for_role(role: Role, config: Config) -> str:
    """
    Връща конфигурирания модел за дадена роля.

    Role.JUDGE е нарочно изключение: `model_judge` не е в _REQUIRED_VARS (config.py) —
    празна стойност значи "MODEL_JUDGE не е зададен в .env", и в този случай Съдията
    преизползва model_critic. И двете роли правят сравнителна преценка срещу корпуса, не
    пишат текст — разумно подразбиране, докато клъстерът не зададе отделен модел изрично.
    """
    field = _ROLE_TO_CONFIG_FIELD[role]
    value = getattr(config, field)
    if role is Role.JUDGE and not value:
        return config.model_critic
    return value


def supports_vision(role: Role) -> bool:
    """Само Media Picker получава снимки в тази версия на конвейера."""
    return role is Role.MEDIA


# Ред на "най-подходящ" резервен модел при пълен провал на основния — по близост на
# задачата до дадената роля. Media е накрая, защото vision не е гарантиран за друга
# роля; текстовите роли (планер/писател/критик/консолидатор) си приличат достатъчно,
# за да е приемливо да поемат чужда задача като последна мярка.
_FALLBACK_PRIORITY: dict[Role, tuple[Role, ...]] = {
    Role.PLANNER: (Role.WRITER, Role.CONSOLIDATOR, Role.CRITIC, Role.MEDIA),
    Role.WRITER: (Role.CONSOLIDATOR, Role.CRITIC, Role.PLANNER, Role.MEDIA),
    Role.CRITIC: (Role.CONSOLIDATOR, Role.WRITER, Role.PLANNER, Role.MEDIA),
    Role.CONSOLIDATOR: (Role.WRITER, Role.CRITIC, Role.PLANNER, Role.MEDIA),
    Role.MEDIA: (Role.WRITER, Role.CONSOLIDATOR, Role.CRITIC, Role.PLANNER),
    # Съдията прави сравнителна преценка, като Критика — най-близката резервна задача.
    Role.JUDGE: (Role.CRITIC, Role.CONSOLIDATOR, Role.WRITER, Role.PLANNER, Role.MEDIA),
}


def fallback_chain(role: Role, config: Config) -> list[str]:
    """
    Пълната резервна верига за дадена роля: най-напред `MODEL_FALLBACK`, после моделите
    на другите роли по низходяща близост до задачата на `role`. Дубликати по стойност
    (два field-а сочещи към едно и също име на модел) се премахват, за да не се
    пробва едно и също извикване два пъти.
    """
    chain = [config.model_fallback]
    for other_role in _FALLBACK_PRIORITY[role]:
        chain.append(model_for_role(other_role, config))

    seen: set[str] = set()
    deduped: list[str] = []
    for model in chain:
        if model not in seen:
            seen.add(model)
            deduped.append(model)
    return deduped
