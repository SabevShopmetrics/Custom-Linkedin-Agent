"""
Логване за конвейера. Допълва meta.json (структурирани данни за LLM извикванията) с
хронологичен, четим лог на целия run — стъпки, предупреждения, финален статус — записан
в posts/<име>/run.log. Логът се пише в самата папка на поста (не в output/), защото
трябва да съществува дори когато конвейерът спре преди output/ да бъде създадена.

Файлът се append-ва между пускания (не се трие/презаписва) — самата история на опитите
върху даден пост е част от одитната следа, като всичко останало в тази система.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "linkedin_agent"

# Handler-и, които преживяват handlers.clear() в setup_run_logger. Нужни са на
# интерактивния режим (src/repl.py): той се закача веднъж, а run_pipeline се вика
# по веднъж на всеки промпт и всеки път пренастройва logger-а от нула.
_persistent_handlers: list[logging.Handler] = []


def register_persistent_handler(handler: logging.Handler) -> None:
    """
    Закача handler, който остава закачен през всички следващи run-ове. Докато има
    поне един такъв, setup_run_logger НЕ добавя своя конзолен handler — този, който
    се е регистрирал, поема конзолния изход и сам решава как да го форматира.
    """
    if handler not in _persistent_handlers:
        _persistent_handlers.append(handler)


def unregister_persistent_handler(handler: logging.Handler) -> None:
    if handler in _persistent_handlers:
        _persistent_handlers.remove(handler)
    logging.getLogger(LOGGER_NAME).removeHandler(handler)


def setup_run_logger(post_dir: Path, *, console_level: int = logging.WARNING) -> logging.Logger:
    """
    Конфигурира logger с два handler-а:
      - файлов (run.log в post_dir) на INFO ниво — пълна хронология на run-а
      - конзолен на `console_level` (по подразбиране WARNING) — за да не дублира
        съществуващите print() съобщения в cli.py, само предупреждения/грешки се
        показват допълнително на конзолата в реално време

    Ако има регистрирани persistent handler-и (виж register_persistent_handler),
    конзолният handler се пропуска — иначе интерактивният режим би показвал всяко
    предупреждение два пъти, в два различни формата.

    Извиква се наново при всяко run_pipeline() извикване — handlers се изчистват
    първо, за да не се трупат дубликати при повторни извиквания в един процес (напр.
    тестове).
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # изчиства предишни handler-и, за да не се дублират логове при повторно извикване
    logger.propagate = False  # не изкачвай до root logger-а — иначе дублира извежданията му

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Файловият handler винаги пише пълна хронология на INFO ниво, независимо кой режим
    # (CLI/REPL) вика функцията — mode="a", защото историята на опитите остава.
    post_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(post_dir / "run.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if _persistent_handlers:
        # Интерактивният режим вече е закачил свой handler — той поема конзолата.
        for handler in _persistent_handlers:
            logger.addHandler(handler)
    else:
        # Извън интерактивен режим (cli.py generate/check): обикновен handler на
        # console_level, за да не се дублират print()-ите на самия cli.py.
        console_handler = logging.StreamHandler()
        console_handler.setLevel(console_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """За модули, които логват без да са отговорни за настройката (planner/writer/etc.)."""
    return logging.getLogger(LOGGER_NAME)
