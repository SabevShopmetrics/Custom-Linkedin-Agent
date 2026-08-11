"""
Терминален вход/изход. Регресията, която налага sanitize_input: пренасочен stdin на
Windows се декодира с конзолния codepage и кирилицата стига до Python като surrogate
байтове; първият опит за запис на briefing.md гърмеше с UnicodeEncodeError и събаряше
цялата сесия (наблюдавано 2026-08-11).
"""

from __future__ import annotations

import sys

from src.terminal import force_utf8_io, sanitize_input


def test_sanitize_input_replaces_surrogates_instead_of_raising():
    cleaned = sanitize_input("Клъстер\udc81ът")

    cleaned.encode("utf-8")  # не бива да гърми — това е същината
    assert "Клъстер" in cleaned
    assert "\udc81" not in cleaned


def test_sanitize_input_leaves_valid_cyrillic_untouched():
    assert sanitize_input("Стажантска програма 2026") == "Стажантска програма 2026"


def test_force_utf8_io_survives_streams_without_reconfigure(monkeypatch):
    class PlainStream:
        """Например io.StringIO под pytest — няма reconfigure()."""

    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, PlainStream())

    force_utf8_io()  # без изключение


def test_force_utf8_io_ignores_streams_that_refuse_reconfigure(monkeypatch):
    class StubbornStream:
        def reconfigure(self, **_kwargs):
            raise OSError("не мога")

    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, StubbornStream())

    force_utf8_io()  # най-добро усилие, никога не чупи CLI-то
