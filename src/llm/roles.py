"""
Роля → модел. Логиката на конвейера иска модел по роля (Role enum), никога по име —
съпоставянето живее изцяло в конфигурацията (.env), тук е само мапингът към полето
в Config, за да може да се смени модел без да се пипа pipeline код.
"""

from __future__ import annotations

from enum import Enum

from src.config import Config


class Role(str, Enum):
    PLANNER = "planner"
    WRITER = "writer"
    MEDIA = "media"
    CRITIC = "critic"
    CONSOLIDATOR = "consolidator"


_ROLE_TO_CONFIG_FIELD: dict[Role, str] = {
    Role.PLANNER: "model_planner",
    Role.WRITER: "model_writer",
    Role.MEDIA: "model_media",
    Role.CRITIC: "model_critic",
    Role.CONSOLIDATOR: "model_consolidator",
}


def model_for_role(role: Role, config: Config) -> str:
    """Връща конфигурирания модел за дадена роля."""
    field = _ROLE_TO_CONFIG_FIELD[role]
    return getattr(config, field)


def supports_vision(role: Role) -> bool:
    """Само Media Picker получава снимки в тази версия на конвейера."""
    return role is Role.MEDIA
