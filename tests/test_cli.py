"""
--brief замества нуждата от 'new' + ръчно редактиране на briefing.md — директно от команден
ред. Тестваме само аргумент-обработката в cli.py (папка/файл/force), не самия конвейер —
load_config/LLMClient/run_pipeline са подменени с фалшиви версии.
"""

from __future__ import annotations

from argparse import Namespace

import src.cli as cli
from src.config import Config


def _fake_config() -> Config:
    return Config(
        openwebui_base_url="https://example.test",
        openwebui_api_key="key",
        model_planner="p",
        model_writer="w",
        model_media="m",
        model_critic="c",
        model_consolidator="co",
        model_fallback="f",
    )


def test_generate_with_brief_creates_folder_and_writes_briefing(tmp_path, monkeypatch):
    post_dir = tmp_path / "new-post"
    captured_force = {}

    monkeypatch.setattr(cli, "load_config", lambda: _fake_config())
    monkeypatch.setattr(cli, "LLMClient", lambda config: object())

    def fake_run_pipeline(pd, client, *, force, mode=None, wait_for_final_check_seconds=0.0):
        captured_force["value"] = force

        class Result:
            status = "ready"
            post_path = pd / "output" / "post.md"
            output_dir = pd / "output"
            warnings = []
            open_notes = []
            merged_from = ["post_i.md", "post_ii.md"]
            applied_fixes = []
            resolution_summary = None

        return Result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    args = Namespace(post_dir=str(post_dir), force=False, brief="Проведохме тестово събитие вчера.")
    exit_code = cli.cmd_generate(args)

    assert exit_code == 0
    assert (post_dir / "photos").is_dir()
    assert (post_dir / "briefing.md").read_text(encoding="utf-8").strip() == "Проведохме тестово събитие вчера."
    assert captured_force["value"] is True  # --brief форсира пълно преизчисление


def test_generate_without_brief_and_missing_folder_fails_before_touching_config(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda: calls.append("load_config") or _fake_config())

    args = Namespace(post_dir=str(tmp_path / "does-not-exist"), force=False, brief=None)
    exit_code = cli.cmd_generate(args)

    assert exit_code == 1
    assert calls == []  # не бива да е стигнало до конфигурация/модел изобщо


def test_generate_without_brief_uses_explicit_force_flag(tmp_path, monkeypatch):
    post_dir = tmp_path / "existing-post"
    post_dir.mkdir()
    captured_force = {}

    monkeypatch.setattr(cli, "load_config", lambda: _fake_config())
    monkeypatch.setattr(cli, "LLMClient", lambda config: object())

    def fake_run_pipeline(pd, client, *, force, mode=None, wait_for_final_check_seconds=0.0):
        captured_force["value"] = force

        class Result:
            status = "ready"
            post_path = pd / "output" / "post.md"
            output_dir = pd / "output"
            warnings = []
            open_notes = []
            merged_from = ["post_i.md", "post_ii.md"]
            applied_fixes = []
            resolution_summary = None

        return Result()

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)

    args = Namespace(post_dir=str(post_dir), force=True, brief=None)
    cli.cmd_generate(args)

    assert captured_force["value"] is True


def test_no_subcommand_opens_the_repl_not_the_tui(monkeypatch):
    """
    Потребителска инструкция 2026-08-25: обратно към readline REPL по подразбиране (както
    отпреди 2026-08-17) — full-screen TUI грабва alt-screen буфера на терминала, което
    изключва native scrollback (за разлика от Claude Code CLI и от REPL-а тук). TUI остава
    достъпно изрично през `tui`.
    """
    calls = []
    monkeypatch.setattr(cli, "cmd_chat", lambda args: calls.append("chat") or 0)
    monkeypatch.setattr(cli, "cmd_tui", lambda args: calls.append("tui") or 0)
    monkeypatch.setattr(cli, "force_utf8_io", lambda: None)

    exit_code = cli.main([])

    assert exit_code == 0
    assert calls == ["chat"]
