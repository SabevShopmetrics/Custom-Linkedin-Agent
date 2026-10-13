"""Споделени помощници за тестовете — без реални мрежови извиквания никъде."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from typing import Any

import pytest

from src.brand.loader import BrandKnowledge
from src.config import Config

# --------------------------------------------------------------------------- твърда защита
#
# Изрично искане на потребителя (2026-10-09 и 2026-10-12): тестовете НИКОГА не бива да пипат
# работния плот на човека, който ги пуска — нито браузъра, нито клипборда („Готов пост" в
# клипборда), нито файловия мениджър, нито диалог за файлове. Функционалният fixture по-долу
# подменя функциите на агента; ТАЗИ защита е на ниво процес и хваща всичко, което се изплъзне
# (стойност по подразбиране, свързана при import, нов код, който вика subprocess директно):
# командата изобщо не се изпълнява, а сесията на pytest пада с ясно съобщение кой я е поискал.

_DESKTOP_COMMANDS = {
    "powershell", "pwsh", "clip", "pbcopy", "xclip", "xsel", "wl-copy",
    "xdg-open", "open", "explorer", "start", "cmd",
}
_desktop_violations: list[str] = []


def _command_name(args: Any) -> str:
    first = args if isinstance(args, str) else (args[0] if args else "")
    name = os.path.basename(str(first).strip().split()[0] if str(first).strip() else "").lower()
    return name.removesuffix(".exe")


class _GuardedPopen(subprocess.Popen):
    def __init__(self, args, *pargs, **kwargs):
        if _command_name(args) in _DESKTOP_COMMANDS:
            _desktop_violations.append(f"subprocess: {args!r}")
            args = [sys.executable, "-c", "pass"]  # безобидна замяна — нищо не стига до плота
            kwargs.pop("shell", None)
        super().__init__(args, *pargs, **kwargs)


def _blocked(label: str):
    def _record(*args, **kwargs):
        _desktop_violations.append(f"{label}: {args!r}")
        return True

    return _record


def pytest_configure(config):
    subprocess.Popen = _GuardedPopen  # subprocess.run и всичко останало минават през Popen
    for name in ("open", "open_new", "open_new_tab"):
        setattr(webbrowser, name, _blocked(f"webbrowser.{name}"))
    if hasattr(os, "startfile"):
        os.startfile = _blocked("os.startfile")  # type: ignore[attr-defined]
    try:
        from tkinter import filedialog

        filedialog.askopenfilenames = _blocked("tkinter.filedialog")
    except ImportError:
        pass


def pytest_sessionfinish(session, exitstatus):
    if _desktop_violations:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        lines = "\n".join(f"  - {item}" for item in _desktop_violations)
        message = (
            "\nТЕСТ СЕ ОПИТА ДА ПИПНЕ РАБОТНИЯ ПЛОТ (браузър/клипборд/файлов мениджър) — блокирано:\n"
            f"{lines}\nПодменете функцията в теста (виж _no_desktop_side_effects в tests/conftest.py).\n"
        )
        if reporter:
            reporter.write_line(message)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _no_desktop_side_effects(monkeypatch, tmp_path):
    """
    Нито един тест не бива да пипа работния плот на човека, който ги пуска (research-plan-36,
    Част 0): преди тази защита обикновен `pytest` отваряше реални прозорци в браузъра
    (cli.cmd_generate → webbrowser.open; Repl → os.startfile), презаписваше клипборда и
    създаваше истински `.env` в корена на репото от отговора на въпроса „да го създам ли?"
    — последното правеше и два REPL теста зависими от реда на пускане.
    """
    import webbrowser

    import src.repl as repl_module
    import src.tui as tui_module

    monkeypatch.setattr(webbrowser, "open", lambda *args, **kwargs: True)
    for module in (repl_module, tui_module):
        monkeypatch.setattr(module, "_open_in_file_manager", lambda path: None)
        monkeypatch.setattr(module, "copy_to_clipboard", lambda text: True)
    # Repl._ask_for_api_key пише ключа и в os.environ — delenv тук гарантира, че monkeypatch
    # връща оригиналното състояние след всеки тест, вместо ключът да изтече към следващите.
    monkeypatch.delenv("OPENWEBUI_API_KEY", raising=False)
    monkeypatch.setattr(repl_module, "ENV_PATH", tmp_path / "_no_such_dir" / ".env")
    monkeypatch.setattr(repl_module, "ENV_EXAMPLE_PATH", tmp_path / "_no_such_dir" / ".env.example")

    yield

    # Стабилност (2026-10-12): фоновата финална проверка [7] (runner._start_async_final_check)
    # е daemon нишка, която може да надживее теста и да пише/вика callback-ове, докато следващият
    # тест вече тече. Изчаква се детерминистично тук, вместо всеки тест да „поспи" 0.3s.
    _join_background_checks()


def _join_background_checks(timeout: float = 10.0) -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name.startswith("final-check-") and thread is not threading.current_thread():
            thread.join(timeout=timeout)


def make_config(**overrides: Any) -> Config:
    base = dict(
        openwebui_base_url="https://example.test",
        openwebui_api_key="test-key",
        model_planner="planner-model",
        model_writer="writer-model",
        model_media="media-model",
        model_critic="critic-model",
        model_consolidator="consolidator-model",
        model_fallback="fallback-model",
        request_timeout_seconds=5,
        model_max_retries=3,
        invalid_response_max_retries=2,
        max_image_bytes=4 * 1024 * 1024,
    )
    base.update(overrides)
    return Config(**base)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def content_response(text: str) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": text}}]})


class StubClient:
    """LLMClient заместител за тестове на pipeline стъпки — гърми, ако някой го извика."""

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Моделът не трябваше да бъде извикван за тази стъпка (skip логиката е нарушена).")


def make_brand_knowledge(**overrides: Any) -> BrandKnowledge:
    base = dict(
        stable_context="",
        facts=None,
        facts_available=False,
        members=[],
        good_examples=[],
        bad_examples=[],
        audit=[],
    )
    base.update(overrides)
    return BrandKnowledge(**base)
