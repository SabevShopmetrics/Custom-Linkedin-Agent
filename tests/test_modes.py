"""
pipeline/modes.py — режимите за дължина на поста (Кадър/Фокус/Панорама). Чисто-функционален
модул (без LLM/диск), затова тестовете тук са прости unit тестове на резолвацията.
"""

from __future__ import annotations

from src.pipeline import modes


def test_default_mode_has_no_word_override():
    """'Фокус' (по подразбиране) не пипа диапазона от корпуса — виж докстринга на MODES."""
    default = modes.get_mode(modes.DEFAULT_MODE)
    assert default.words_min is None
    assert default.words_max is None
    assert default.instruction == ""


def test_kadar_is_shorter_than_panorama():
    kadar = modes.get_mode("kadar")
    panorama = modes.get_mode("panorama")
    assert kadar.words_max < panorama.words_min


def test_resolve_mode_accepts_cyrillic_and_english_aliases():
    assert modes.resolve_mode("кадър") == "kadar"
    assert modes.resolve_mode("SHORT") == "kadar"
    assert modes.resolve_mode("Панорама") == "panorama"
    assert modes.resolve_mode("long") == "panorama"
    assert modes.resolve_mode("фокус") == "fokus"


def test_resolve_mode_rejects_unknown_text():
    assert modes.resolve_mode("бла бла") is None
    assert modes.resolve_mode("") is None
    assert modes.resolve_mode(None) is None


def test_get_mode_falls_back_to_default_for_unknown_key():
    assert modes.get_mode("не-съществува") == modes.get_mode(modes.DEFAULT_MODE)
    assert modes.get_mode(None) == modes.get_mode(modes.DEFAULT_MODE)
