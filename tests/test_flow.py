"""web-ui-4: папка → пост сами, „Преглед" с един поглед, сигнали при провал на писането. Без мрежа."""

from __future__ import annotations

import io
import os
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import requests
from PIL import Image

from src.pipeline.util import read_json, write_json
from src.web import alerts, flow, library
from src.web import server as web_server

H = {"X-Requested-With": "linkedin-agent"}
TODAY = date(2026, 10, 14)


def _jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 110, 110)).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())


# --------------------------------------------------------------------------- заглавни редове и име на папка


def test_header_lines_are_read_and_stripped_from_the_brief():
    meta, body = flow.parse_header("Кога: 21.10.2026 10:00\nРежим: кадър\nЗаглавие: Хакатон\n---\n42 ученици участваха.")
    assert meta == {"кога": "21.10.2026 10:00", "режим": "кадър", "заглавие": "Хакатон"}
    assert body == "42 ученици участваха."
    assert flow.when_from("x", meta, today=TODAY) == ("2026-10-21", "10:00")
    assert flow.mode_from(meta) == "kadar"
    assert flow.title_from("x", meta) == "Хакатон"


def test_plain_text_without_header_is_all_brief():
    meta, body = flow.parse_header("Вчера проведохме среща: 30 души.\nВтори ред.")
    assert meta == {} and body.startswith("Вчера проведохме среща: 30 души.")


def test_date_and_time_come_from_the_folder_name():
    assert flow.when_from("2026-10-21 10.00 Хакатон", {}, today=TODAY) == ("2026-10-21", "10:00")
    assert flow.when_from("21.10 Хакатон", {}, today=TODAY) == ("2026-10-21", None)
    assert flow.title_from("2026-10-21 10.00 Хакатон", {}) == "Хакатон"
    assert flow.when_from("Хакатон", {"кога": "28 октомври", "час": "9:30"}, today=TODAY) == ("2026-10-28", "09:30")


def test_issues_and_lanes():
    sched = {"time_warning": "казва „вчера“", "needs_brief": False}
    problems = flow.issues(has_text=True, meta={"status": "ready_with_notes", "open_notes": ["а"], "warnings": []},
                           sched=sched, minors=1, job_error=None, source_changed=True, published=False)
    assert [p["kind"] for p in problems] == ["minor", "time", "source", "notes"]
    assert flow.lane(running=False, queued=False, has_text=True, published=False, problems=problems, needs_brief=False) == "review"
    only_notes = [p for p in problems if p["kind"] == "notes"]
    assert flow.lane(running=False, queued=False, has_text=True, published=False, problems=only_notes, needs_brief=False) == "ready"
    failed = flow.issues(has_text=False, meta={}, sched={}, minors=0, job_error="таймаут", source_changed=False, published=False)
    assert failed[0]["kind"] == "failed"
    assert flow.lane(running=False, queued=False, has_text=False, published=False, problems=failed, needs_brief=False) == "review"
    overdue = flow.issues(has_text=True, meta={"status": "ready"}, sched={}, minors=0, job_error=None,
                          source_changed=False, published=False, overdue=True)
    assert overdue[0]["kind"] == "due"


# --------------------------------------------------------------------------- сървър


def _fake_pipeline(post_dir, client, *, mode=None, on_draft=None, **kwargs):
    (post_dir / "output").mkdir(parents=True, exist_ok=True)
    brief = (post_dir / "briefing.md").read_text(encoding="utf-8")
    if "ПРОВАЛ" in brief:
        raise RuntimeError("моделът не отговори")
    (post_dir / "output" / "post.md").write_text(f"Пост: {brief.strip()}\n", encoding="utf-8")
    write_json(post_dir / "output" / "meta.json", {"status": "ready", "open_notes": [], "warnings": [], "applied_fixes": []})


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server.runner, "run_pipeline", _fake_pipeline)
    folder = tmp_path / "LinkedIn"
    folder.mkdir()
    posts = tmp_path / "posts"
    posts.mkdir()
    library.set_folder(posts, str(folder))
    app = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True, max_jobs=2)
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app, posts, folder
    httpd.shutdown()
    httpd.server_close()
    app.close()


def _wait_idle(app, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not app.running_jobs() and not app.queue:
            return
        time.sleep(0.05)
    raise AssertionError("задачите не приключиха")


def _post_folder(folder: Path, name: str, text: str, *, old: bool = False) -> None:
    _jpeg(folder / name / "a.jpg")
    (folder / name / "текст.txt").write_text(text, encoding="utf-8")
    if old:
        past = time.time() - 30 * 86400
        for item in (folder / name).iterdir():
            os.utime(item, (past, past))


def test_auto_ingest_takes_only_new_folders_and_schedules_them(live):
    base, app, posts, folder = live
    _post_folder(folder, "Стара папка", "Стар пост.", old=True)
    assert app.ingest_tick() == []  # изключено по подразбиране
    app.set_auto_ingest(True)
    _post_folder(folder, "2099-11-05 11.30 Нов член", "Режим: кадър\n\nПриветстваме фирма „Морски софтуер“.")
    created = app.ingest_tick()
    assert len(created) == 1
    _wait_idle(app)
    web = read_json(posts / created[0] / "web.json")
    assert web["scheduled_for"] == "2099-11-05" and web["scheduled_time"] == "11:30"
    assert web["title"] == "Нов член" and web["mode"] == "kadar" and web["source"] == "folder"
    assert (posts / created[0] / "output" / "post.md").read_text(encoding="utf-8").startswith("Пост: Приветстваме")
    assert app.ingest_tick() == []  # не се взима втори път

    overview = requests.get(f"{base}/api/overview", timeout=5).json()
    assert [w["title"] for w in overview["folder_waiting"]] == ["Стара папка"]  # старата чака ръчно „Вземи"
    assert overview["lanes"]["ready"][0]["title"] == "Нов член"

    r = requests.post(f"{base}/api/ingest", json={"paths": ["Стара папка"]}, headers=H, timeout=5)
    assert r.status_code == 201
    _wait_idle(app)
    assert requests.get(f"{base}/api/overview", timeout=5).json()["folder_waiting"] == []


def test_changed_folder_text_is_flagged_and_rewrite_uses_it(live):
    _, app, posts, folder = live
    app.set_auto_ingest(True)
    _post_folder(folder, "Хакатон", "42 ученици.")
    post_id = app.ingest_tick()[0]
    _wait_idle(app)
    (folder / "Хакатон" / "текст.txt").write_text("45 ученици.", encoding="utf-8")
    app.ingest_tick()
    assert read_json(posts / post_id / "web.json")["source_changed"] is True
    view = app.overview()
    card = next(c for c in view["lanes"]["review"] if c["id"] == post_id)
    assert card["issues"][0]["kind"] == "source"
    job = app.rewrite_from_source(post_id)
    _wait_idle(app)
    assert "45 ученици" in (posts / job.post_id / "output" / "post.md").read_text(encoding="utf-8")
    assert not read_json(posts / job.post_id / "web.json")["source_changed"]


def test_failed_writing_of_scheduled_post_raises_alert(live):
    _, app, posts, folder = live
    app.set_auto_ingest(True)
    _post_folder(folder, "2099-11-05 Провал", "ПРОВАЛ при писането")
    post_id = app.ingest_tick()[0]
    _wait_idle(app)
    kinds = [a["kind"] for a in alerts.list_alerts(posts)]
    assert kinds == ["writing_failed"]
    assert app.overview()["lanes"]["review"][0]["issues"][0]["kind"] == "failed"
    assert post_id


def test_relative_time_words_are_fixed_automatically_once(live, monkeypatch):
    _, app, posts, folder = live

    def pipeline(post_dir, client, **kwargs):
        (post_dir / "output").mkdir(parents=True, exist_ok=True)
        (post_dir / "output" / "post.md").write_text("Вчера проведохме хакатон.\n", encoding="utf-8")
        write_json(post_dir / "output" / "meta.json", {"status": "ready", "open_notes": [], "warnings": []})

    revisions = []

    def revision(post_dir, client, instruction, **kwargs):
        revisions.append(instruction)
        (post_dir / "output" / "post.md").write_text("На 13 октомври проведохме хакатон.\n", encoding="utf-8")

    monkeypatch.setattr(web_server.runner, "run_pipeline", pipeline)
    monkeypatch.setattr(web_server.runner, "run_revision", revision)
    app.set_auto_ingest(True)
    _post_folder(folder, "2099-11-05 Хакатон", "Вчера проведохме хакатон.")
    post_id = app.ingest_tick()[0]
    _wait_idle(app)
    time.sleep(0.2)
    _wait_idle(app)
    assert len(revisions) == 1 and "2099-11-05" in revisions[0]
    card = next(c for c in app.overview()["lanes"]["ready"] if c["id"] == post_id)
    assert not any(i["kind"] == "time" for i in card["issues"])
    assert alerts.list_alerts(posts) == []  # оправено само — няма за какво да се вдига сигнал
