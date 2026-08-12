"""
`cli bench` — базовата линия от план 11, Фаза 1.

Двете неща, които трябва да са гарантирани: инструментът НЕ вика модел (иначе базовата
линия става платена и никой не я пуска), и разпознава двата провала, които сегашната верига
допуска мълчаливо — изхвърлен факт спрямо Post I, и „синтез", който е дословно копие.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.quality import bench

RICH = 'На 5 август 2026 г. в офиса на бул. „Сливница" 189 се събраха 12 представители.'
POOR = "Срещата бе фокусирана върху ключови етапи от подготовката."


def _make_run(
    root: Path,
    name: str,
    *,
    post_i: str,
    post_ii: str | None = None,
    official: str | None = None,
    status: str = "ready",
    critique: dict | None = None,
) -> Path:
    post_dir = root / name
    (post_dir / "output").mkdir(parents=True, exist_ok=True)
    (post_dir / "post_i.md").write_text(post_i, encoding="utf-8")
    if post_ii is not None:
        (post_dir / "post_ii.md").write_text(post_ii, encoding="utf-8")
    if official is not None:
        (post_dir / "output" / "post.md").write_text(official, encoding="utf-8")
    (post_dir / "output" / "meta.json").write_text(
        json.dumps({"status": status, "merged_from": ["post_i.md"]}, ensure_ascii=False), encoding="utf-8"
    )
    if critique is not None:
        (post_dir / "critique.md").write_text(
            "# Критика\n\n```json\n" + json.dumps(critique, ensure_ascii=False) + "\n```\n",
            encoding="utf-8",
        )
    return post_dir


def test_bench_never_calls_a_model(tmp_path, monkeypatch):
    """
    Пази свойството, което прави базовата линия годна за употреба: безплатна и повтаряема.
    Всяко HTTP извикване от този път би го отменило.
    """
    import requests

    def explode(*args, **kwargs):
        raise AssertionError("bench не бива да прави мрежово извикване.")

    monkeypatch.setattr(requests, "post", explode)
    monkeypatch.setattr(requests, "get", explode)
    _make_run(tmp_path, "run-1", post_i=RICH, official=RICH)

    report = bench.collect(tmp_path)

    assert len(report.posts) == 1


def test_detects_when_the_official_post_lost_facts_from_post_i(tmp_path):
    _make_run(tmp_path, "degraded", post_i=RICH, post_ii=POOR, official=POOR)
    _make_run(tmp_path, "kept", post_i=RICH, post_ii=RICH, official=RICH)

    aggregate = bench.collect(tmp_path).aggregate()

    assert aggregate["runs"] == 2
    assert aggregate["official_has_fewer_facts_than_post_i"] == 1


def test_detects_synthesis_that_returned_one_version_verbatim(tmp_path):
    """
    Наблюдавано в 7 от 20 реални пускания: Консолидаторът връща Версия II дума по дума.
    Изходът изглежда нормален, стъпката не гърми, синтез няма.
    """
    _make_run(tmp_path, "bypassed", post_i=RICH, post_ii=POOR, official=POOR)
    _make_run(tmp_path, "synthesized", post_i=RICH, post_ii=POOR, official=RICH + " " + POOR)

    aggregate = bench.collect(tmp_path).aggregate()

    assert aggregate["runs_with_second_post"] == 2
    assert aggregate["synthesis_skipped"] == 1


def test_run_without_second_post_is_not_counted_as_failed_synthesis(tmp_path):
    """Липсващият втори пост значи „Критикът не намери какво да се запълва", не провал."""
    _make_run(tmp_path, "single", post_i=RICH, official=RICH)

    aggregate = bench.collect(tmp_path).aggregate()

    assert aggregate["runs_with_second_post"] == 0
    assert aggregate["synthesis_skipped"] == 0


def test_mechanical_critic_findings_are_counted_separately_from_tone(tmp_path):
    """
    Целта за механичните е нула: броенето на емоджи/думи/хаштагове е в кода от план 10 и
    промптът изрично забранява на Критика да го коментира. Реален случай 2026-08-11 —
    Критикът твърди „само 1 емоджи", когато са 4.
    """
    _make_run(
        tmp_path,
        "run-1",
        post_i=RICH,
        official=RICH,
        critique={
            "verdict": "needs_revision",
            "facts": [{"claim": "нещо", "source": "UNVERIFIED", "note": ""}],
            "tone_issues": [
                "Броят на емоджитата в поста е само 1, което нарушава минималния брой.",
                "Изразът 'ключови етапи' е клише — заменете с факт от брифа.",
            ],
            "rule_violations": [{"rule": "gdpr", "detail": "организация без съгласие"}],
            "summary": "х",
        },
    )

    post = bench.collect(tmp_path).posts[0]

    assert post.findings_mechanical == 1
    assert post.findings_tone == 1
    assert post.findings_factual == 1
    assert post.findings_rules == 1


def test_only_prefix_filters_to_a_controlled_brief_set(tmp_path):
    _make_run(tmp_path, "bench-01", post_i=RICH, official=RICH)
    _make_run(tmp_path, "random-work-in-progress", post_i=RICH, official=RICH)

    report = bench.collect(tmp_path, only="bench-")

    assert [p.name for p in report.posts] == ["bench-01"]


def test_directory_without_post_i_is_skipped_not_crashed(tmp_path):
    (tmp_path / "empty-run").mkdir()
    _make_run(tmp_path, "real-run", post_i=RICH, official=RICH)

    report = bench.collect(tmp_path)

    assert [p.name for p in report.posts] == ["real-run"]


def test_underscore_directories_are_ignored(tmp_path):
    """posts/_example е шаблон, не пускане."""
    _make_run(tmp_path, "_example", post_i=RICH, official=RICH)

    assert bench.collect(tmp_path).posts == []


def test_save_and_compare_round_trip(tmp_path):
    _make_run(tmp_path, "run-1", post_i=RICH, official=RICH)
    report = bench.collect(tmp_path)
    path = bench.save(report, tmp_path / "reports" / "baseline.json")

    loaded = bench.load(path)

    assert loaded["aggregate"]["runs"] == 1
    assert "Спрямо предишния отчет" in bench.render(report, loaded)


def test_load_of_a_missing_or_broken_report_returns_none(tmp_path):
    (tmp_path / "broken.json").write_text("{не е json", encoding="utf-8")

    assert bench.load(tmp_path / "missing.json") is None
    assert bench.load(tmp_path / "broken.json") is None


def test_render_without_runs_does_not_crash(tmp_path):
    output = bench.render(bench.collect(tmp_path))

    assert "Няма пускания за анализ." in output
