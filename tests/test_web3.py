"""web-ui-3: папка на компютъра, асистент само за LinkedIn, насрочване и одобрение, живи режими.

Без мрежа: асистентът получава подменен модел, конвейерът — подменена функция (като test_web.py).
"""

from __future__ import annotations

import io
import threading
import time
from datetime import date, datetime
from pathlib import Path

import pytest
import requests
from PIL import Image

from src.pipeline.util import read_json, write_json
from src.web import alerts, assistant, insights, library
from src.web import server as web_server

H = {"X-Requested-With": "linkedin-agent"}
TODAY = date(2026, 10, 14)


def _jpeg(path: Path, color=(10, 110, 110)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (60, 40), color).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())


@pytest.fixture
def folder(tmp_path) -> Path:
    root = tmp_path / "LinkedIn"
    _jpeg(root / "2026-10-20 среща" / "a.jpg")
    _jpeg(root / "2026-10-20 среща" / "b.jpg")
    _jpeg(root / "Стажанти" / "c.jpg")
    _jpeg(root / "LinkedIn постове" / "ignored.jpg")  # наш изход — не е албум
    (root / "график ноември.csv").write_text("Дата;Бриф\n20.10.2026;Среща на членовете\n", encoding="utf-8-sig")
    (root / "бележки.txt").write_text("просто бележки", encoding="utf-8")
    return root


# --------------------------------------------------------------------------- папка


def test_scan_finds_albums_with_dates_and_schedules(folder):
    scan = library.scan(folder)
    albums = {a["path"]: a for a in scan["albums"]}
    assert set(albums) == {"2026-10-20 среща", "Стажанти"}
    assert albums["2026-10-20 среща"]["count"] == 2
    assert albums["2026-10-20 среща"]["date"] == "2026-10-20"
    assert albums["Стажанти"]["date"] is None
    assert [s["path"] for s in scan["schedules"]] == ["график ноември.csv"]  # бележки.txt не е график


def test_resolve_refuses_paths_outside_the_folder(folder):
    with pytest.raises(library.LibraryError):
        library.resolve(folder, "../..")
    assert library.resolve(folder, "Стажанти/c.jpg").is_file()


def test_resolve_photos_by_folder_name_and_by_date(folder):
    by_name = library.resolve_photos(folder, ["стажанти"], None)
    assert [p.name for p in by_name] == ["c.jpg"]
    by_date = library.resolve_photos(folder, [], "2026-10-20")
    assert [p.name for p in by_date] == ["a.jpg", "b.jpg"]


def test_set_folder_validates_and_persists(tmp_path, folder):
    posts = tmp_path / "posts"
    with pytest.raises(library.LibraryError):
        library.set_folder(posts, "relative/path")
    with pytest.raises(library.LibraryError):
        library.set_folder(posts, str(tmp_path / "няма-я"))
    library.set_folder(posts, str(folder))
    assert library.get_folder(posts) == folder


def test_library_thumbnail_is_cached_and_original_untouched(tmp_path, folder):
    source = folder / "Стажанти" / "c.jpg"
    before = source.read_bytes()
    thumb = library.thumbnail(source, tmp_path / "cache")
    assert thumb and thumb.is_file()
    assert library.thumbnail(source, tmp_path / "cache") == thumb
    assert source.read_bytes() == before


# --------------------------------------------------------------------------- режими


def test_mode_options_read_tone_range_and_own_stats(tmp_path):
    insights.clear_cache()
    tone = tmp_path / "tone.md"
    tone.write_text("- Дължина: 120–380 думи, но не като цел\n", encoding="utf-8")
    posts = tmp_path / "posts"
    post = posts / "2026-10-01-x"
    (post / "output").mkdir(parents=True)
    (post / "output" / "post.md").write_text("едно две три четири\nвтори ред", encoding="utf-8")
    write_json(post / "web.json", {"mode": "kadar"})
    options = {m["key"]: m for m in insights.mode_options(posts, tone)}
    assert options["fokus"]["hint"] == "120–380 думи"  # прочетено от tone.md, не зашито
    assert options["kadar"]["hint"] == "40–90 думи"
    assert options["kadar"]["used"] == 1 and options["kadar"]["avg_words"] == 6
    assert options["kadar"]["sample"] == "едно две три четири"
    assert all(m["best_for"] for m in options.values())
    insights.clear_cache()


def test_suggestions_come_from_real_albums_and_empty_slots():
    scan = {"albums": [{"path": "2026-10-20 среща", "name": "2026-10-20 среща", "count": 2, "date": "2026-10-20"}], "schedules": []}
    posts = [{"id": "x", "needs_brief": True, "scheduled_for": "2026-11-03", "title": "Празно"}]
    kinds = [s["kind"] for s in insights.suggestions(posts, scan, "2026-10-14")]
    assert kinds[0] == "album" and "slot" in kinds


# --------------------------------------------------------------------------- асистент: правила в код


def test_clean_plan_drops_past_dates_and_moves_model_ideas_out_of_brief():
    items = [
        {"date": "2026-10-01", "title": "Минало", "brief": "нещо"},
        {"date": "2026-11-03", "time": "9.5", "title": "Идея", "brief": "Кратко въведение за месеца и професионално развитие"},
        {"date": "20.11.2026", "time": "14:30", "title": "Уъркшоп", "brief": "Уъркшоп за ИИ в бизнеса на 20 ноември", "mode": "кадър", "album": "../x"},
    ]
    said = "На 20 ноември имаме уъркшоп за ИИ в бизнеса"
    plan, notes = assistant.clean_plan(items, TODAY, {"Стажанти"}, source_text=said)
    assert [p["date"] for p in plan] == ["2026-11-03", "2026-11-20"]
    idea, workshop = plan
    assert idea["brief"] == "" and idea["needs_brief"] and idea["idea"].startswith("Кратко въведение")
    assert idea["time"] == "10:00"  # невалиден час → подразбиране
    assert workshop["brief"] and not workshop["needs_brief"]
    assert workshop["mode"] == "kadar" and workshop["time"] == "14:30"
    assert workshop["album"] is None  # непознат албум (и опит за излизане от папката) отпада
    assert any("миналото" in n for n in notes) and any("идеи" in n for n in notes)


def test_clean_plan_warns_when_post_is_after_the_deadline_in_its_brief():
    plan, notes = assistant.clean_plan(
        [{"date": "2026-11-17", "title": "Покана", "brief": "Уъркшоп на 20 ноември, записване до 15 ноември"}],
        TODAY, set(),
    )
    assert "2026-11-15" in plan[0]["warning"]
    assert notes


class _FakeAssistantModel:
    def __init__(self, turn):
        self.turn = turn
        self.prompts = []

    def complete(self, role, prompt, *, system=None, schema=None, **kwargs):
        self.prompts.append((role, prompt, system))
        return self.turn


def test_off_topic_gets_fixed_refusal_and_no_plan(tmp_path):
    thread = assistant.create_thread(tmp_path)
    model = _FakeAssistantModel(assistant.AssistantTurn(
        on_topic=False, reply="Париж", plan=[assistant.PlanItem(date="2026-11-03", title="x")]))
    result = assistant.respond(tmp_path, thread["id"], "Столицата на Франция?", client=model,
                               calendar=[], library_scan=None, today=TODAY)
    agent = result["messages"][-1]
    assert agent["text"] == assistant.OFF_TOPIC_REPLY and "plan" not in agent
    assert result["title"] == "Нов разговор"  # отказ не именува разговора


def test_respond_sends_calendar_and_albums_and_stores_plan(tmp_path):
    thread = assistant.create_thread(tmp_path)
    model = _FakeAssistantModel(assistant.AssistantTurn(on_topic=True, reply="Ето план.", plan=[
        assistant.PlanItem(date="2026-11-05", title="Среща", brief="", album="2026-10-20 среща", needs_brief=True),
    ]))
    scan = {"name": "LinkedIn", "albums": [{"path": "2026-10-20 среща", "count": 2, "date": "2026-10-20"}], "schedules": []}
    calendar = [{"title": "Вече насрочен", "scheduled_for": "2026-11-03", "scheduled_time": "10:00"}]
    result = assistant.respond(tmp_path, thread["id"], "План за ноември", client=model,
                               calendar=calendar, library_scan=scan, today=TODAY)
    _, prompt, system = model.prompts[0]
    assert "САМО за LinkedIn" in system
    assert "2026-11-03 10:00 · Вече насрочен" in prompt and "2026-10-20 среща · 2" in prompt
    plan = result["messages"][-1]["plan"]
    assert plan[0]["album"] == "2026-10-20 среща" and plan[0]["needs_brief"]
    assert assistant.load_thread(tmp_path, thread["id"])["title"] == "План за ноември"


# --------------------------------------------------------------------------- HTTP: план → календар, одобрение, час


def _fake_pipeline(post_dir, client, *, mode=None, on_draft=None, **kwargs):
    (post_dir / "output").mkdir(parents=True, exist_ok=True)
    (post_dir / "output" / "post.md").write_text("Готов пост.\n", encoding="utf-8")
    write_json(post_dir / "output" / "meta.json", {"status": "ready", "open_notes": [], "warnings": [], "applied_fixes": []})


@pytest.fixture
def live(tmp_path, monkeypatch, folder):
    monkeypatch.setattr(web_server.runner, "run_pipeline", _fake_pipeline)
    posts = tmp_path / "posts"
    posts.mkdir()
    library.set_folder(posts, str(folder))
    app = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True, max_jobs=2)
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app, posts
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


def test_apply_plan_creates_slots_and_writes_only_briefed_posts(live):
    base, app, posts = live
    thread = assistant.create_thread(posts)
    items = [
        {"date": "2099-11-05", "time": "10:00", "title": "Среща", "brief": "Среща на членовете — 30 души", "album": "2026-10-20 среща"},
        {"date": "2099-11-07", "time": "11:00", "title": "Празно място", "brief": "", "idea": "представяне на член"},
    ]
    r = requests.post(f"{base}/api/assistant/{thread['id']}/apply", json={"index": 0, "items": items}, headers=H, timeout=5)
    assert r.status_code == 201, r.text
    _wait_idle(app)
    listed = {p["title"]: p for p in requests.get(f"{base}/api/posts", timeout=5).json()["posts"]}
    meeting, empty = listed["Среща"], listed["Празно място"]
    assert meeting["scheduled_for"] == "2099-11-05" and meeting["status"] == "ready"
    assert meeting["album"] == "2026-10-20 среща"
    assert sorted(p.name for p in (posts / meeting["id"] / "photos").iterdir()) == ["a.jpg", "b.jpg"]
    assert empty["needs_brief"] and empty["status"] is None and empty["scheduled_time"] == "11:00"
    assert empty["idea"] == "представяне на член"
    note = assistant.load_thread(posts, thread["id"])["messages"][-1]
    assert note["kind"] == "note" and note["batch"]


def test_approval_needs_text_and_date_and_is_voided_by_edit(live):
    base, app, posts = live
    post_id = app.create_post("Бриф", None)
    url = f"{base}/api/posts/{post_id}/schedule"
    assert requests.put(url, json={"approved": True}, headers=H, timeout=5).status_code == 409  # няма текст
    _fake_pipeline(posts / post_id, None)
    assert requests.put(url, json={"approved": True}, headers=H, timeout=5).status_code == 409  # няма дата
    info = requests.put(url, json={"date": "2099-01-02", "time": "9:05", "approved": True}, headers=H, timeout=5).json()
    assert info["approved"] and info["scheduled_time"] == "09:05"
    app.save_text(post_id, "Друг текст от човека.")
    state = requests.get(f"{base}/api/posts/{post_id}", timeout=5).json()["schedule"]
    assert not state["approved"] and state["approval_stale"]
    assert requests.put(url, json={"time": "25:00"}, headers=H, timeout=5).status_code == 400


def test_scheduler_reminds_once_in_draft_mode_and_skips_unapproved(live):
    _, app, posts = live
    approved = app.create_post("А", None)
    _fake_pipeline(posts / approved, None)
    app.set_schedule(approved, when="2026-11-03", at="10:00", approved=True)
    plain = app.create_post("Б", None)
    _fake_pipeline(posts / plain, None)
    app.set_schedule(plain, when="2026-11-03", at="10:00")

    assert app.scheduler_tick(datetime(2026, 11, 3, 9, 59)) == []  # още не е часът
    assert app.scheduler_tick(datetime(2026, 11, 3, 10, 1)) == [approved]
    assert app.scheduler_tick(datetime(2026, 11, 3, 10, 2)) == []  # само веднъж
    messages = read_json(posts / approved / "web.json")["messages"]
    assert "Време е за публикуване" in messages[-1]["text"]
    assert not (posts / approved / "output" / "published.md").exists()  # чернова: нищо не излиза само


def test_scheduler_does_not_publish_when_server_was_down_for_hours(live, monkeypatch):
    _, app, posts = live
    post_id = app.create_post("А", None)
    _fake_pipeline(posts / post_id, None)
    app.set_schedule(post_id, when="2026-11-03", at="10:00", approved=True)
    calls = []
    monkeypatch.setattr(app, "publish_settings",
                        lambda: web_server.publisher.PublishSettings(mode="linkedin", access_token="x", org_id="1"))
    monkeypatch.setattr(app, "publish", lambda *a, **k: calls.append(a))
    app.scheduler_tick(datetime(2026, 11, 3, 20, 0))
    assert calls == []
    assert "мина, докато агентът беше спрян" in read_json(posts / post_id / "web.json")["messages"][-1]["text"]
    missed = alerts.list_alerts(posts)
    assert [a["kind"] for a in missed] == ["publish_missed"] and missed[0]["level"] == "error"


def test_library_endpoints_read_schedule_and_make_post_from_album(live):
    base, _, posts = live
    lib = requests.get(f"{base}/api/library", timeout=5).json()
    assert lib["can_choose"] and lib["library"]["name"] == "LinkedIn"
    parsed = requests.post(f"{base}/api/library/schedule", json={"path": "график ноември.csv"}, headers=H, timeout=5).json()
    assert parsed["rows"][0]["photo_count"] == 2  # снимките по датата — от диска, без качване
    post = requests.post(f"{base}/api/library/album", json={"path": "Стажанти"}, headers=H, timeout=5).json()
    assert [p["name"] for p in post["photos"]] == ["c.jpg"] and post["pending"] == ["c.jpg"]
    image = requests.get(f"{base}/api/library/image", params={"path": "Стажанти/c.jpg"}, timeout=5)
    assert image.status_code == 200 and image.headers["Content-Type"] == "image/jpeg"
    assert requests.get(f"{base}/api/library/image", params={"path": "../../x.jpg"}, timeout=5).status_code == 404


def test_static_modules_are_served_as_javascript(live):
    base, _, _ = live
    for name in ("app.js", "core.js", "store.js", "chat.js", "assistant.js", "calendar.js", "library.js", "board.js", "views.css"):
        r = requests.get(f"{base}/static/{name}", timeout=5)
        assert r.status_code == 200, name
    page = requests.get(base + "/", timeout=5).text
    assert 'type="module"' in page and "/static/app.js" in page


# --------------------------------------------------------------------------- папка = пост; „пиши сега, публикувай по график"


def test_folder_with_text_and_photos_is_a_ready_post(tmp_path):
    root = tmp_path / "Постове"
    _jpeg(root / "Пост 1 - Хакатон" / "a.jpg")
    (root / "Пост 1 - Хакатон" / "бележка.txt").write_text("друго", encoding="utf-8")
    (root / "Пост 1 - Хакатон" / "текст.txt").write_text("Вчера 42 ученици участваха в хакатон.", encoding="utf-8")
    _jpeg(root / "Пост 2" / "b.jpg")
    (root / "Пост 2" / "пост.md").write_bytes("Нов член: фирма „Морски софтуер“.".encode("cp1251"))
    scan = library.scan(root)
    albums = {a["path"]: a for a in scan["albums"]}
    assert albums["Пост 1 - Хакатон"]["brief"].startswith("Вчера 42 ученици")  # „текст" печели пред „бележка"
    assert albums["Пост 2"]["brief"] == "Нов член: фирма „Морски софтуер“."  # cp1251 от стар Notepad
    assert scan["schedules"] == []  # текстът на пост-папка не е график


def test_plan_finds_album_by_title_when_model_omits_it_and_uses_folder_text():
    # Живо, 2026-10-14: моделът позна постовете по заглавие, но върна album=null и празен бриф.
    albums = {
        "Пост 1 - Хакатон": "Вчера ученически хакатон „Код за Варна“ с 42 ученици.",
        "Пост 2 - Нов член": "Приветстваме нов член: фирма „Морски софтуер“ ООД.",
    }
    items = [
        {"date": "2026-10-21", "title": "Резултати от ученически хакатон „Код за Варна“", "album": None},
        {"date": "2026-10-28", "title": "Нов член", "album": "Пост 2 – Нов член (1)"},
    ]
    plan, _ = assistant.clean_plan(items, TODAY, albums, source_text="насрочи постовете")
    assert [p["album"] for p in plan] == ["Пост 1 - Хакатон", "Пост 2 - Нов член"]
    assert plan[0]["brief"].startswith("Вчера ученически") and not plan[0]["needs_brief"]


def test_relative_time_words_are_flagged_only_when_publish_day_differs():
    warn = insights.relative_time_warning("Вчера проведохме хакатон. Утре — награждаване.", "2026-10-21", "2026-10-14")
    assert "вчера" in warn and "утре" in warn
    assert insights.relative_time_warning("Вчера проведохме хакатон.", "2026-10-14", "2026-10-14") is None
    assert insights.relative_time_warning("На 20 октомври проведохме хакатон.", "2026-10-21", "2026-10-14") is None


def test_schedule_info_carries_time_warning_and_plan_gets_short_name(live):
    base, app, posts = live
    post_id = app.create_post("Бриф", None)
    (posts / post_id / "output").mkdir(parents=True, exist_ok=True)
    (posts / post_id / "output" / "post.md").write_text("Утре ви очакваме.\n", encoding="utf-8")
    info = app.set_schedule(post_id, when="2099-01-05", at="10:00")
    assert info["time_warning"] and "утре" in info["time_warning"]
    assert web_server._plan_name([{"date": "2026-10-21"}, {"date": "2026-11-04"}]) == "2 поста · 21 окт – 4 ное"


# --------------------------------------------------------------------------- сигнали


def test_alerts_dedupe_resolve_and_deliver_outside(tmp_path):
    sent = []
    first = alerts.raise_alert(tmp_path, kind="publish_failed", level="error", message="401", key="k", post_id="p",
                               sender=lambda a: sent.append(a["id"]) or {"webhook": "ok 200"})
    again = alerts.raise_alert(tmp_path, kind="publish_failed", level="error", message="401 пак", key="k", post_id="p",
                               sender=lambda a: sent.append(a["id"]) or {})
    assert again["id"] == first["id"] and again["count"] == 2
    for _ in range(50):
        if alerts.list_alerts(tmp_path)[0]["delivery"]:
            break
        time.sleep(0.02)
    assert sent == [first["id"]]  # същият проблем не се праща навън втори път
    assert alerts.list_alerts(tmp_path)[0]["delivery"] == {"webhook": "ok 200"}
    assert alerts.summary(tmp_path)["errors"] == 1
    assert alerts.resolve_for_post(tmp_path, "p", ("publish_failed",)) == 1
    assert alerts.summary(tmp_path)["open"] == 0


def test_failed_publish_raises_alert_and_success_resolves_it(live, monkeypatch):
    base, app, posts = live
    post_id = app.create_post("А", None)
    _fake_pipeline(posts / post_id, None)
    monkeypatch.setattr(app, "publish_settings",
                        lambda: web_server.publisher.PublishSettings(mode="linkedin", access_token="x", org_id="1"))

    def boom(*args, **kwargs):
        raise web_server.publisher.PublishError("LinkedIn отказа токена (401).")

    monkeypatch.setattr(web_server.publisher, "publish", boom)
    r = requests.post(f"{base}/api/posts/{post_id}/publish", json={}, headers=H, timeout=5)
    assert r.status_code == 502
    info = requests.get(f"{base}/api/info", timeout=5).json()["alerts"]
    assert info["open"] == 1 and info["errors"] == 1 and "401" in info["latest"]["message"]

    monkeypatch.setattr(web_server.publisher, "publish",
                        lambda *a, **k: web_server.publisher.PublishResult(mode="linkedin", url="https://x"))
    assert requests.post(f"{base}/api/posts/{post_id}/publish", json={}, headers=H, timeout=5).status_code == 200
    assert requests.get(f"{base}/api/info", timeout=5).json()["alerts"]["open"] == 0


def test_config_and_token_warnings_before_the_publish_hour(live, monkeypatch):
    _, app, posts = live
    post_id = app.create_post("А", None)
    _fake_pipeline(posts / post_id, None)
    app.set_schedule(post_id, when="2026-11-03", at="10:00", approved=True)
    monkeypatch.setattr(app, "publish_settings", lambda: web_server.publisher.PublishSettings(mode="linkedin"))
    monkeypatch.setenv("LINKEDIN_TOKEN_EXPIRES", "2026-11-06")
    app.scheduler_tick(datetime(2026, 11, 2, 12, 0))  # 22 ч. преди часа, токенът изтича след 4 дни
    kinds = sorted(a["kind"] for a in alerts.list_alerts(posts))
    assert kinds == ["publish_config", "token_expiring"]
    app.scheduler_tick(datetime(2026, 11, 2, 12, 1))
    assert len(alerts.list_alerts(posts)) == 2  # без дублиране


def test_alert_endpoints_list_resolve_and_test(live):
    base, _, _ = live
    created = requests.post(f"{base}/api/alerts/test", json={}, headers=H, timeout=5).json()
    assert created["sending"] is False  # тестовият сървър няма канал навън — никаква мрежа
    listed = requests.get(f"{base}/api/alerts", timeout=5).json()["alerts"]
    assert listed[0]["kind"] == "test"
    assert requests.post(f"{base}/api/alerts/{listed[0]['id']}/resolve", json={}, headers=H, timeout=5).json()["open"] == 0


# --------------------------------------------------------------------------- асистент = едно място за нов пост


def test_described_event_is_written_right_away(live):
    base, app, posts = live
    turn = assistant.AssistantTurn(on_topic=True, reply="Пиша поста.", write_now=True, plan=[
        assistant.PlanItem(date="", title="Среща", brief="Вчера 30 души от 15 фирми се срещнаха във Варна."),
    ])
    app._client_factory = lambda: _FakeAssistantModel(turn)
    thread = assistant.create_thread(posts)
    r = requests.post(f"{base}/api/assistant/{thread['id']}/messages",
                      json={"text": "Вчера 30 души от 15 фирми се срещнаха във Варна.", "mode": "kadar"},
                      headers=H, timeout=5)
    last = r.json()["messages"][-1]
    assert last["write_now"] and last["post_id"]
    _wait_idle(app)
    state = requests.get(f"{base}/api/posts/{last['post_id']}", timeout=5).json()
    assert state["mode"] == "kadar" and state["post"]["status"] == "ready"
    assert state["schedule"]["scheduled_for"] is None  # „сега" — без насрочване
