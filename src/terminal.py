"""
Терминален вход/изход на UTF-8. Windows-специфично, но безвредно на Linux/Mac.

Windows конзолата по подразбиране е cp1252/cp866 — кирилица на изхода гърми, а
кирилица на ВХОДА се декодира с грешен codec и стига до Python като surrogate
байтове (`\\udc81`), които после гърмят при запис на файл. И двете посоки трябва
да са UTF-8, не само изходът.
"""

from __future__ import annotations

import sys


def force_utf8_io() -> None:
    """Пренасочва stdin/stdout/stderr към UTF-8, ако терминалът го поддържа."""
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, None), (sys.stderr, None)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if errors is None:
                reconfigure(encoding="utf-8")
            else:
                reconfigure(encoding="utf-8", errors=errors)
        except Exception:  # pragma: no cover — най-добро усилие, никога не бива да чупи CLI-то
            pass


def sanitize_input(text: str) -> str:
    """
    Изчиства surrogate байтове от вход, който въпреки force_utf8_io е бил декодиран с
    друг codec (пренасочен stdin от файл в cp1251, напр.). Без това един такъв символ
    сваля цялата сесия при записа на briefing.md — предпочитаме „?" в брифа пред
    прекъсната сесия.
    """
    return text.encode("utf-8", "replace").decode("utf-8")
