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


def setup_run_logger(post_dir: Path, *, console_level: int = logging.WARNING) -> logging.Logger:
    """
    Конфигурира logger с два handler-а:
      - файлов (run.log в post_dir) на INFO ниво — пълна хронология на run-а
      - конзолен на `console_level` (по подразбиране WARNING) — за да не дублира
        съществуващите print() съобщения в cli.py, само предупреждения/грешки се
        показват допълнително на конзолата в реално време

    Извиква се наново при всяко run_pipeline() извикване — handlers се изчистват
    първо, за да не се трупат дубликати при повторни извиквания в един процес (напр.
    тестове).
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    post_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(post_dir / "run.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """За модули, които логват без да са отговорни за настройката (planner/writer/etc.)."""
    return logging.getLogger(LOGGER_NAME)
