"""web-ui-5: туториалът се пуска сам само веднъж, „Настройки" събира универсалното, статичните файлове съществуват."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest
import requests

from src.pipeline.util import read_json
from src.web import server as web_server

H = {"X-Requested-With": "linkedin-agent"}
STATIC = Path(web_server.__file__).parent / "static"


@pytest.fixture
def live(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    app = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True, max_jobs=2)
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", posts
    httpd.shutdown()
    httpd.server_close()
    app.close()


def test_tutorial_is_offered_until_finished_then_never_again(live):
    base, posts = live
    assert requests.get(f"{base}/api/info").json()["tutorial"]["done"] is False

    r = requests.put(f"{base}/api/settings/tutorial", json={"state": "finished", "step": 21}, headers=H)
    assert r.status_code == 200 and r.json()["done"] is True

    info = requests.get(f"{base}/api/info").json()["tutorial"]
    assert info == {"done": True, "state": "finished", "at": info["at"], "step": 21}
    # Помни се на сървъра (posts/_settings.json), не в браузъра — друг браузър също не го вижда.
    assert read_json(posts / "_settings.json")["tutorial"]["state"] == "finished"


def test_skipping_counts_as_done_and_reset_offers_it_again(live):
    base, _ = live
    requests.put(f"{base}/api/settings/tutorial", json={"state": "skipped", "step": 3}, headers=H)
    assert requests.get(f"{base}/api/info").json()["tutorial"]["state"] == "skipped"

    requests.put(f"{base}/api/settings/tutorial", json={"state": "reset"}, headers=H)
    assert requests.get(f"{base}/api/info").json()["tutorial"]["done"] is False


def test_unknown_tutorial_state_is_refused(live):
    base, _ = live
    r = requests.put(f"{base}/api/settings/tutorial", json={"state": "maybe"}, headers=H)
    assert r.status_code == 400


def test_tutorial_change_needs_the_ui_header(live):
    base, _ = live
    r = requests.put(f"{base}/api/settings/tutorial", json={"state": "finished"})
    assert r.status_code in (403, 400)
    assert requests.get(f"{base}/api/info").json()["tutorial"]["done"] is False


def test_settings_page_collects_the_universal_values(live):
    base, _ = live
    data = requests.get(f"{base}/api/settings").json()
    assert set(data) >= {"tutorial", "folder", "auto_ingest", "publish_mode", "max_jobs", "alert_channels", "speed"}
    assert data["folder"] is None and data["auto_ingest"] is False and data["max_jobs"] == 2


def test_new_static_files_are_served_and_linked():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="/static/tour.css"' in html and 'id="view-settings"' in html
    for name in ("tutorial.js", "settings.js", "tour.css"):
        assert (STATIC / name).is_file()


def test_tutorial_covers_every_module_and_the_folder_format():
    source = (STATIC / "tutorial.js").read_text(encoding="utf-8")
    chapters = set(re.findall(r"chapter: '([^']+)'", source))
    assert chapters == {"Начало", "Преглед", "Агентите", "Асистент", "Пост", "Папка", "Календар", "Сигнали", "Настройки"}
    routes = set(re.findall(r"route: '([^']+)'", source))
    assert routes >= {"#/", "#/asistent", "#/nov", "#/papka", "#/kalendar", "#/signali", "#/nastroiki"}
    assert "Най-ефективният формат на папката" in source and "Всичко работи и се приема" in source
    # Форматът, който туториалът препоръчва, наистина се чете от кода (flow.py).
    from src.web import flow

    meta, body = flow.parse_header("Кога: 30.10.2026 11:00\nРежим: фокус\nЗаглавие: Стажанти\n---\nШестима студенти…")
    assert flow.when_from("2026-10-30 11.00 Стажанти в Морски софтуер", meta) == ("2026-10-30", "11:00")
    assert flow.when_from("2026-10-30 11.00 Стажанти в Морски софтуер", {}) == ("2026-10-30", "11:00")
    assert flow.title_from("2026-10-30 11.00 Стажанти в Морски софтуер", {}) == "Стажанти в Морски софтуер"
    assert flow.mode_from(meta) == "fokus" and body == "Шестима студенти…"


def test_every_team_stage_matches_a_real_pipeline_stage():
    """Лентата „Агентите“ показва ролята по етапа — етапите идват от src/repl.py и worker.py."""
    from src.repl import _HUMAN_STAGES

    core = (STATIC / "core.js").read_text(encoding="utf-8")
    patterns = re.findall(r"stage: /([^/]+)/", core)
    stages = [s for s in _HUMAN_STAGES.values() if s] + ["Последна проверка на готовия текст…"]
    for stage in stages:
        assert any(re.search(p, stage) for p in patterns), stage


# --------------------------------------------------------------------------- „Качи папка" (inbox)


@pytest.fixture
def app(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    application = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True, max_jobs=2)
    yield application
    application.close()


def test_uploaded_folder_becomes_a_scheduled_post_by_its_name(app):
    from datetime import date, timedelta

    day = (date.today() + timedelta(days=5)).isoformat()
    post = app.inbox_post(f"{day} 11.00 Стажанти в Морски софтуер", "Шестима студенти започнаха стаж.")
    web = read_json(app.posts_dir / post["id"] / "web.json")
    assert post["when"] == day and post["at"] == "11:00" and post["has_brief"] is True
    assert web["title"] == "Стажанти в Морски софтуер" and web["source"] == "upload"
    assert (app.posts_dir / post["id"] / "briefing.md").read_text(encoding="utf-8").strip() == "Шестима студенти започнаха стаж."


def test_header_lines_win_and_are_not_part_of_the_brief(app):
    from datetime import date, timedelta

    day = date.today() + timedelta(days=9)
    text = f"Кога: {day:%d.%m.%Y} 16:30\nРежим: кадър\nЗаглавие: Уебинар\n---\nУебинар за облака."
    post = app.inbox_post("Посещение без дата", text)
    assert (post["when"], post["at"], post["mode"], post["title"]) == (day.isoformat(), "16:30", "kadar", "Уебинар")
    assert "Кога" not in (app.posts_dir / post["id"] / "briefing.md").read_text(encoding="utf-8")


def test_folder_without_text_waits_for_a_brief_and_is_not_written(app):
    post = app.inbox_post("Снимки от срещата", "")
    web = read_json(app.posts_dir / post["id"] / "web.json")
    assert post["has_brief"] is False and web["needs_brief"] is True
    assert app.inbox_start(post["id"]) == {"queued": False}


def test_past_date_is_not_scheduled_and_is_reported(app):
    post = app.inbox_post("2020-01-05 10.00 Стар повод", "Нещо от 2020.")
    assert post["when"] is None and post["past"] is True


def test_start_queues_writing(app, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(app, "generate", lambda post_id, brief, mode, **kw: started.append(post_id))
    post = app.inbox_post("Без дата", "Кратък бриф.")
    assert app.inbox_start(post["id"]) == {"queued": True}
    assert started == [post["id"]]


def test_custom_controls_and_folder_upload_are_wired():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    files_js = (STATIC / "files.js").read_text(encoding="utf-8")
    assert "controls.init()" in app_js and "inbox.groupFolders(files)" in files_js
    assert "webkitdirectory" in (STATIC / "inbox.js").read_text(encoding="utf-8")


def test_second_server_on_the_same_port_is_refused(tmp_path):
    """Живо 2026-10-14: на Windows втори сървър тихо заемаше порта, а отговаряше старият."""
    posts = tmp_path / "posts"
    posts.mkdir()
    app = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True)
    first = web_server.make_server(app, "127.0.0.1", 0)
    try:
        with pytest.raises(OSError):
            web_server.make_server(app, "127.0.0.1", first.server_address[1])
    finally:
        first.server_close()
        app.close()


def test_folder_upload_is_one_batch_with_a_dashboard(app):
    from datetime import date, timedelta

    day = (date.today() + timedelta(days=3)).isoformat()
    batch = app.inbox_batch("papka-20-sabitiya")
    first = app.inbox_post(f"{day} 10.00 Среща", "Среща на членовете.", batch["id"])
    second = app.inbox_post("Снимки без текст", "", batch["id"])
    status = app.batch_status(batch["id"])
    assert status["kind"] == "upload" and [i["post_id"] for i in status["items"]] == [first["id"], second["id"]]
    assert read_json(app.posts_dir / first["id"] / "web.json")["batch"] == batch["id"]
    item = status["items"][0]
    assert item["time"] == "10:00" and item["approved"] is False and item["date"] == day
    summary = status["summary"]
    assert summary["first_date"] == day and summary["eta_seconds"] is None and summary["autofixed"] == 0


def test_dashboard_summary_counts_autofixes_and_eta(app, monkeypatch):
    from src.pipeline.util import write_json

    batch = app.inbox_batch("пакет")
    post = app.inbox_post("Пост", "Бриф.", batch["id"])
    out = app.posts_dir / post["id"] / "output"
    out.mkdir(parents=True)
    (out / "post.md").write_text("Текст на поста.\n", encoding="utf-8")
    write_json(out / "meta.json", {"status": "ready_with_notes", "open_notes": ["Недоказано твърдение: \"X\" (няма)"],
                                   "warnings": [], "pipeline_seconds": 180,
                                   "applied_fixes": ["Махнато след проверката: Y", "Махнато след проверката: Z"]})
    status = app.batch_status(batch["id"])
    item = status["items"][0]
    assert item["autofixed"] == 2 and "Агентът махна сам 2" in item["digest"]
    assert status["summary"]["avg_seconds"] == 180 and status["summary"]["autofixed"] == 2
    # Опашка от 4 при 2 агента и средно 180 s → 2 „вълни“ → 360 s.
    monkeypatch.setattr(app, "_item_state", lambda pid: "queued")
    fake = [{"state": "queued"}] * 4 + [{"state": "done", "seconds": 180}]
    assert app._batch_summary(fake)["eta_seconds"] == 360


def test_page_and_server_agree_on_the_ui_version():
    """Иначе „Нужен е рестарт" излиза при всеки (или при никой) сървър."""
    store_js = (STATIC / "store.js").read_text(encoding="utf-8")
    assert f"UI_VERSION = '{web_server.UI_VERSION}'" in store_js


def test_info_reports_the_ui_version(live):
    base, _ = live
    assert requests.get(f"{base}/api/info").json()["ui_version"] == web_server.UI_VERSION
