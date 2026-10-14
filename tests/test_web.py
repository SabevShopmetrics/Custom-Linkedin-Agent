"""Уеб интерфейсът (src/web/) — качване/ZIP, избор на снимки, HTTP API с подменен конвейер."""

from __future__ import annotations

import base64
import json
import sys
import io
import threading
import time
import zipfile
from pathlib import Path

import pytest
import requests
from PIL import Image

from src.pipeline.util import read_json, write_json
from src.web import server as web_server
from src.web import uploads

H = {"X-Requested-With": "linkedin-agent"}


def _jpeg(color=(200, 30, 30), size=(40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _upload(post_dir: Path, name: str, data: bytes) -> uploads.UploadReport:
    incoming = post_dir / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    temp = incoming / "upload.tmp"
    temp.write_bytes(data)
    return uploads.add_upload(post_dir, name, temp)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


# --------------------------------------------------------------------------- uploads


def test_safe_filename_strips_paths_and_reserved_characters():
    assert uploads.safe_filename("../../etc/passwd.JPG") == "passwd.jpg"
    assert uploads.safe_filename("C:\\Users\\x\\снимка 1.jpeg") == "снимка 1.jpeg"
    assert uploads.safe_filename('a<b>:c?.png') == "a_b__c_.png"
    assert uploads.safe_filename("") == "snimka"


def test_single_image_goes_to_photos(tmp_path):
    report = _upload(tmp_path, "IMG_1.JPG", _jpeg())
    assert report.added == ["IMG_1.jpg"]
    assert (tmp_path / "photos" / "IMG_1.jpg").is_file()
    assert not list((tmp_path / ".incoming").iterdir())  # временният файл е почистен


def test_fake_image_is_rejected(tmp_path):
    report = _upload(tmp_path, "virus.jpg", b"not really a jpeg")
    assert report.added == []
    assert report.skipped and "не се отваря" in report.skipped[0][1]


def test_zip_extracts_images_skips_junk_and_flattens_paths(tmp_path):
    data = _zip({
        "event/01.jpg": _jpeg(),
        "event/../../escape.jpg": _jpeg(),
        "__MACOSX/event/._01.jpg": b"junk",
        "event/notes.txt": b"hello",
        "event/iphone.HEIC": b"heic",
        "event/.DS_Store": b"x",
    })
    report = _upload(tmp_path, "event.zip", data)
    assert sorted(report.added) == ["01.jpg", "escape.jpg"]
    assert not (tmp_path.parent / "escape.jpg").exists()
    reasons = dict(report.skipped)
    assert "не е снимка" in reasons["notes.txt"]
    assert "HEIC" in reasons["iphone.HEIC"]
    assert "._01.jpg" not in reasons and ".DS_Store" not in reasons


def test_zip_with_same_names_gets_unique_names(tmp_path):
    report = _upload(tmp_path, "a.zip", _zip({"day1/IMG.jpg": _jpeg(), "day2/IMG.jpg": _jpeg((0, 0, 255))}))
    assert sorted(report.added) == ["IMG-2.jpg", "IMG.jpg"]


def test_cyrillic_names_from_windows_zip_are_recovered(tmp_path):
    # Windows Explorer пише имената в cp866 без UTF-8 флага; zipfile сам не може да създаде
    # такъв архив, затова ASCII име със същата дължина се подменя в суровите байтове.
    raw_name = "снимка".encode("cp866")
    data = _zip({"x" * len(raw_name) + ".jpg": _jpeg()}).replace(b"x" * len(raw_name) + b".jpg", raw_name + b".jpg")
    report = _upload(tmp_path, "arhiv.zip", data)
    assert report.added == ["снимка.jpg"]


def test_over_cap_photos_are_parked_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_AGENT_PHOTOS", 2)
    report = _upload(tmp_path, "many.zip", _zip({f"{i}.jpg": _jpeg() for i in range(4)}))
    assert len(report.added) == 2 and len(report.parked) == 2
    assert uploads.included_count(tmp_path) == 2
    assert len(uploads.list_photo_entries(tmp_path)) == 4


def test_toggle_and_selection_round_trip(tmp_path):
    _upload(tmp_path, "a.jpg", _jpeg())
    _upload(tmp_path, "b.jpg", _jpeg())
    write_json(tmp_path / "output" / "meta.json", {"status": "ready", "photos": {"selected": []}})

    assert uploads.set_selection(tmp_path, ["b.jpg", "a.jpg", "nope.jpg"]) == ["b.jpg", "a.jpg"]
    assert uploads.selected_photos(tmp_path) == ["b.jpg", "a.jpg"]
    assert read_json(tmp_path / "output" / "meta.json")["photos"]["human_override"] is True

    # Изключена снимка излиза и от поста
    assert uploads.set_included(tmp_path, "a.jpg", False)
    assert (tmp_path / "photos_off" / "a.jpg").is_file()
    assert uploads.selected_photos(tmp_path) == ["b.jpg"]
    assert not (tmp_path / "output" / "selected" / "a.jpg").exists()
    # Само включени снимки влизат в поста
    assert uploads.set_selection(tmp_path, ["a.jpg"]) == []
    assert uploads.set_included(tmp_path, "a.jpg", True)
    assert (tmp_path / "photos" / "a.jpg").is_file()
    assert not uploads.set_included(tmp_path, "../b.jpg", True)


def test_thumbnail_is_cached(tmp_path):
    _upload(tmp_path, "a.jpg", _jpeg(size=(2000, 1000)))
    thumb = uploads.thumbnail(tmp_path, "a.jpg", "s")
    assert thumb is not None and thumb.is_file()
    with Image.open(thumb) as image:
        assert max(image.size) == uploads.THUMB_SIZES["s"]
    assert uploads.thumbnail(tmp_path, "missing.jpg") is None


# --------------------------------------------------------------------------- HTTP


def _fake_pipeline(post_dir, client, *, force=False, mode=None, on_draft=None, should_cancel=None, **kwargs):
    if on_draft:
        on_draft("Чернова на поста.")
    photos = sorted(p.name for p in (post_dir / "photos").iterdir())[:1]
    (post_dir / "output" / "selected").mkdir(parents=True, exist_ok=True)
    for name in photos:
        (post_dir / "output" / "selected" / name).write_bytes((post_dir / "photos" / name).read_bytes())
    write_json(post_dir / "media.json", {"selected": [{"filename": n, "reason": "хора в действие"} for n in photos]})
    (post_dir / "output" / "post.md").write_text(f"Готов пост ({mode}).\n", encoding="utf-8")
    write_json(post_dir / "output" / "meta.json", {
        "status": "ready_with_notes",
        "open_notes": ["Недоказано твърдение: „40 ученици“"],
        "warnings": [],
        "applied_fixes": ["махнат хаштаг"],
        "photos": {"selected": photos},
        "final_check": {"status": "ok"},
    })


def _fake_revision(post_dir, client, instruction, **kwargs):
    current = (post_dir / "output" / "post.md").read_text(encoding="utf-8").strip()
    (post_dir / "output" / "post.md").write_text(f"{current} [{instruction}]\n", encoding="utf-8")


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server.runner, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(web_server.runner, "run_revision", _fake_revision)
    posts = tmp_path / "posts"
    posts.mkdir()
    app = web_server.WebApp(posts, client_factory=lambda: object(), in_process=True, max_jobs=2)
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, app, posts
    httpd.shutdown()
    httpd.server_close()
    app.close()


def _wait_job(base: str, job_id: str) -> dict:
    for _ in range(100):
        job = requests.get(f"{base}/api/jobs/{job_id}", headers=H, timeout=5).json()["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("задачата не приключи")


def test_index_and_static_are_served(live):
    base, _, _ = live
    page = requests.get(base + "/", timeout=5)
    assert page.status_code == 200 and "LinkedIn агент" in page.text
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert requests.get(base + "/static/app.js", timeout=5).status_code == 200
    assert requests.get(base + "/static/..%2Fserver.py", timeout=5).status_code == 404
    info = requests.get(base + "/api/info", timeout=5).json()
    assert [m["key"] for m in info["modes"]] == ["kadar", "fokus", "panorama"]


def test_full_flow_create_upload_generate_edit_publish(live):
    base, _, posts = live
    created = requests.post(base + "/api/posts", json={"brief": "", "mode": "kadar"}, headers=H, timeout=5)
    assert created.status_code == 201
    post_id = created.json()["id"]
    assert post_id.endswith("nov-post")

    up = requests.post(
        f"{base}/api/posts/{post_id}/upload", params={"name": "event.zip"},
        data=_zip({"a.jpg": _jpeg(), "b.jpg": _jpeg()}), headers=H, timeout=10,
    ).json()
    assert sorted(up["added"]) == ["a.jpg", "b.jpg"]

    thumb = requests.get(f"{base}/api/posts/{post_id}/photos/a.jpg", params={"size": "s"}, timeout=5)
    assert thumb.status_code == 200 and thumb.headers["Content-Type"] == "image/jpeg"

    started = requests.post(
        f"{base}/api/posts/{post_id}/generate", json={"brief": "Среща на клъстера с ученици", "mode": "kadar"},
        headers=H, timeout=5,
    )
    assert started.status_code == 202
    new_id = started.json()["post_id"]
    assert new_id != post_id and "sreshta" in new_id  # временното име е сменено по брифа
    assert _wait_job(base, started.json()["job"]["id"])["state"] == "done"

    state = requests.get(f"{base}/api/posts/{new_id}", timeout=5).json()
    assert state["post"]["text"] == "Готов пост (kadar)."
    assert state["in_post"] == ["a.jpg"]
    assert state["post"]["notes"] and "40 ученици" in state["post"]["notes"][0]
    assert any(p["agent_reason"] == "хора в действие" for p in state["photos"])

    # Ръчна редакция → edited.md, предложението на агента остава непокътнато
    requests.put(f"{base}/api/posts/{new_id}/text", json={"text": "Моят текст."}, headers=H, timeout=5)
    state = requests.get(f"{base}/api/posts/{new_id}", timeout=5).json()
    assert state["post"]["text"] == "Моят текст." and state["post"]["edited"]
    assert (posts / new_id / "output" / "post.md").read_text(encoding="utf-8").startswith("Готов пост")

    # Ревизия тръгва от редакцията на човека, а тя се пази като edited.v1.md
    job = requests.post(f"{base}/api/posts/{new_id}/revise", json={"instruction": "по-кратко"}, headers=H, timeout=5)
    assert _wait_job(base, job.json()["job"]["id"])["state"] == "done"
    state = requests.get(f"{base}/api/posts/{new_id}", timeout=5).json()
    assert state["post"]["text"] == "Моят текст. [по-кратко]" and not state["post"]["edited"]
    assert (posts / new_id / "output" / "edited.v1.md").exists()

    # Ръчен избор на снимки и публикуване
    sel = requests.put(f"{base}/api/posts/{new_id}/selection", json={"names": ["b.jpg", "a.jpg"]}, headers=H, timeout=5)
    assert sel.json()["in_post"] == ["b.jpg", "a.jpg"]
    pub = requests.post(f"{base}/api/posts/{new_id}/published", json={}, headers=H, timeout=5).json()
    assert pub["edit_ratio"] == 0
    assert (posts / new_id / "output" / "published.md").exists()

    # Историята на чата: бриф → пост → молба → нов вариант → бележка за публикуване
    messages = requests.get(f"{base}/api/posts/{new_id}", timeout=5).json()["messages"]
    assert [(m["role"], m.get("kind")) for m in messages] == [
        ("user", None), ("agent", "post"), ("user", None), ("agent", "post"), ("agent", "note"),
    ]
    assert messages[0]["text"] == "Среща на клъстера с ученици" and messages[0]["photos"] == ["a.jpg", "b.jpg"]
    assert messages[2]["text"] == "по-кратко"
    assert messages[3]["text"] == "Моят текст. [по-кратко]"

    listing = requests.get(base + "/api/posts", timeout=5).json()["posts"]
    assert listing[0]["id"] == new_id and listing[0]["published"]


def test_generate_requires_brief_and_writes_errors_humanly(live, monkeypatch):
    base, _, _ = live
    post_id = requests.post(base + "/api/posts", json={"brief": "Нещо"}, headers=H, timeout=5).json()["id"]
    empty = requests.post(f"{base}/api/posts/{post_id}/generate", json={"brief": " "}, headers=H, timeout=5)
    assert empty.status_code == 400 and "изречение" in empty.json()["error"]

    from src.errors import ModelUnavailableError

    def boom(*args, **kwargs):
        raise ModelUnavailableError("writer", "writer-model", 3, detail="HTTP 503")

    monkeypatch.setattr(web_server.runner, "run_pipeline", boom)
    job = requests.post(f"{base}/api/posts/{post_id}/generate", json={"brief": "Нещо"}, headers=H, timeout=5).json()["job"]
    done = _wait_job(base, job["id"])
    assert done["state"] == "error" and done["error"] and "503" not in done["error"]
    messages = requests.get(f"{base}/api/posts/{post_id}", timeout=5).json()["messages"]
    assert messages[-1]["kind"] == "error" and messages[-1]["text"] == done["error"]


def test_several_posts_at_once_up_to_the_limit(live, monkeypatch):
    base, _, _ = live  # max_jobs=2 във fixture-а
    release = threading.Event()

    def slow(post_dir, client, **kwargs):
        release.wait(5)
        _fake_pipeline(post_dir, client, **kwargs)

    monkeypatch.setattr(web_server.runner, "run_pipeline", slow)
    ids = [requests.post(base + "/api/posts", json={"brief": b}, headers=H, timeout=5).json()["id"] for b in ("Първи", "Втори", "Трети")]
    jobs = [requests.post(f"{base}/api/posts/{i}/generate", json={"brief": b}, headers=H, timeout=5) for i, b in zip(ids[:2], ("Първи", "Втори"))]
    assert [j.status_code for j in jobs] == [202, 202]
    again = requests.post(f"{base}/api/posts/{ids[0]}/generate", json={"brief": "Първи"}, headers=H, timeout=5)
    assert again.status_code == 409 and "този пост" in again.json()["error"]
    full = requests.post(f"{base}/api/posts/{ids[2]}/generate", json={"brief": "Трети"}, headers=H, timeout=5)
    assert full.status_code == 409 and "2 поста наведнъж" in full.json()["error"]
    assert len(requests.get(base + "/api/info", timeout=5).json()["jobs"]) == 2
    release.set()
    assert all(_wait_job(base, j.json()["job"]["id"])["state"] == "done" for j in jobs)


def test_delete_moves_post_to_trash_not_gone(live):
    base, _, posts = live
    post_id = requests.post(base + "/api/posts", json={"brief": "За триене"}, headers=H, timeout=5).json()["id"]
    gone = requests.delete(f"{base}/api/posts/{post_id}", headers=H, timeout=5)
    assert gone.status_code == 200
    assert not (posts / post_id).exists()
    assert (posts / "_kosh" / post_id / "briefing.md").read_text(encoding="utf-8").strip() == "За триене"
    assert all(p["id"] != post_id for p in requests.get(base + "/api/posts", timeout=5).json()["posts"])
    assert requests.delete(f"{base}/api/posts/{post_id}", timeout=5).status_code == 403  # без заглавието на интерфейса


def test_stop_kills_the_worker_process_immediately(tmp_path):
    # Истински отделен процес: фалшив работник съобщава етап и после „работи" 60 секунди.
    script = (
        "import json,sys,time\n"
        "print(json.dumps({'stage': 'Проверявам факти, тон и правила…', 'step': 3}), flush=True)\n"
        "time.sleep(60)\n"
    )
    posts = tmp_path / "posts"
    posts.mkdir()
    app = web_server.WebApp(posts, worker_command=lambda spec: [sys.executable, "-c", script])
    try:
        post_id = app.create_post("Бавен пост", None)
        job = app.generate(post_id, "Бавен пост", None)
        for _ in range(100):
            if job.step == 3:
                break
            time.sleep(0.05)
        assert job.stage == "Проверявам факти, тон и правила…" and job.state == "running"

        started = time.monotonic()
        app.cancel(job)
        for _ in range(100):
            if job.state != "running":
                break
            time.sleep(0.05)
        assert job.state == "cancelled" and time.monotonic() - started < 5
        assert job.proc.poll() is not None  # процесът е мъртъв, не чака края на стъпката
        messages = app.post_state(post_id)["messages"]
        assert messages[-1]["kind"] == "stopped" and "Спряно" in messages[-1]["text"]
    finally:
        app.close()


def test_mutations_need_the_interface_header(live):
    base, _, _ = live
    assert requests.post(base + "/api/posts", json={"brief": "x"}, timeout=5).status_code == 403


def test_bad_post_ids_are_not_paths(live):
    base, _, _ = live
    assert requests.get(base + "/api/posts/..%2F..%2Fsrc", timeout=5).status_code == 404
    assert requests.get(base + "/api/posts/_example", timeout=5).status_code == 404


def test_password_protects_everything(tmp_path):
    app = web_server.WebApp(tmp_path, password="тайна", client_factory=lambda: object(), in_process=True)
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert requests.get(base + "/", timeout=5).status_code == 401
        wrong = base64.b64encode("x:грешна".encode()).decode()
        assert requests.get(base + "/", headers={"Authorization": f"Basic {wrong}"}, timeout=5).status_code == 401
        right = base64.b64encode("x:тайна".encode()).decode()
        assert requests.get(base + "/api/info", headers={"Authorization": f"Basic {right}"}, timeout=5).status_code == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        app.close()


def test_refuses_public_host_without_password(tmp_path, capsys):
    assert web_server.serve(host="0.0.0.0", port=0, password=None, posts_dir=tmp_path) == 2
    assert "WEB_PASSWORD" in capsys.readouterr().out


def test_worker_reports_human_stages_from_pipeline_logs(tmp_path):
    from src.logging_utils import register_persistent_handler, setup_run_logger, unregister_persistent_handler
    from src.web import worker

    sent: list[dict] = []
    handler = worker._StageHandler(sent.append)
    register_persistent_handler(handler)
    try:
        log = setup_run_logger(tmp_path)
        log.info("[4] Критик върху Post I...")
        log.info("[4] находка: ...")  # същият етап — не се праща повторно
        log.info("[7] Критик върху предложението")  # фоновата стъпка не мести лентата
    finally:
        unregister_persistent_handler(handler)
    assert sent == [{"stage": "Проверявам факти, тон и правила…", "step": 3}]


def test_worker_main_speaks_json_lines(tmp_path, monkeypatch, capsys):
    from src.web import worker

    monkeypatch.setattr(worker, "run", lambda spec, out, client_factory=None: (out({"draft": "Чернова ü"}), out({"state": "done"}), 0)[-1])
    assert worker.main([json.dumps({"kind": "generate", "post_dir": str(tmp_path)})]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [{"draft": "Чернова ü"}, {"state": "done"}]


def test_cli_registers_web_command():
    from src.cli import build_parser

    args = build_parser().parse_args(["web", "--port", "9999"])
    assert args.port == 9999 and args.open is False


def test_sent_photos_move_into_the_message_and_tray_clears(live):
    base, _, _ = live
    post_id = requests.post(base + "/api/posts", json={"brief": "Събитие"}, headers=H, timeout=5).json()["id"]
    requests.post(f"{base}/api/posts/{post_id}/upload", params={"name": "a.jpg"}, data=_jpeg(), headers=H, timeout=5)
    assert requests.get(f"{base}/api/posts/{post_id}", timeout=5).json()["pending"] == ["a.jpg"]
    job = requests.post(f"{base}/api/posts/{post_id}/generate", json={"brief": "Събитие"}, headers=H, timeout=5).json()
    _wait_job(base, job["job"]["id"])
    state = requests.get(f"{base}/api/posts/{job['post_id']}", timeout=5).json()
    assert state["pending"] == []
    assert state["messages"][0]["photos"] == ["a.jpg"]


def test_old_prompt_with_photos_can_be_recalled_into_another_post(live):
    base, _, posts = live
    first = requests.post(base + "/api/posts", json={"brief": "Първи"}, headers=H, timeout=5).json()["id"]
    requests.post(f"{base}/api/posts/{first}/upload", params={"name": "zala.jpg"}, data=_jpeg(), headers=H, timeout=5)
    job = requests.post(f"{base}/api/posts/{first}/generate", json={"brief": "Първи пост"}, headers=H, timeout=5).json()
    _wait_job(base, job["job"]["id"])
    first = job["post_id"]

    prompts = requests.get(base + "/api/prompts", timeout=5).json()["prompts"]
    assert prompts[0] == {"text": "Първи пост", "post_id": first, "photos": ["zala.jpg"], "at": prompts[0]["at"]}

    second = requests.post(base + "/api/posts", json={"brief": ""}, headers=H, timeout=5).json()["id"]
    pending = requests.post(
        f"{base}/api/posts/{second}/attach", json={"from_post": first, "names": ["zala.jpg", "nope.jpg"]}, headers=H, timeout=5,
    ).json()["pending"]
    assert pending == ["zala.jpg"]
    assert (posts / second / "photos" / "zala.jpg").is_file()
    assert (posts / first / "photos" / "zala.jpg").is_file()  # оригиналът остава


def test_rename_session_changes_only_the_label(live):
    base, _, posts = live
    post_id = requests.post(base + "/api/posts", json={"brief": "Дълъг бриф за събитие"}, headers=H, timeout=5).json()["id"]
    requests.put(f"{base}/api/posts/{post_id}", json={"title": "  CONNEXUS   лекция "}, headers=H, timeout=5)
    assert requests.get(f"{base}/api/posts/{post_id}", timeout=5).json()["title"] == "CONNEXUS лекция"
    listing = requests.get(base + "/api/posts", timeout=5).json()["posts"]
    assert [p["title"] for p in listing if p["id"] == post_id] == ["CONNEXUS лекция"]
    assert (posts / post_id).is_dir()  # папката не е преименувана
    requests.put(f"{base}/api/posts/{post_id}", json={"title": ""}, headers=H, timeout=5)
    assert requests.get(f"{base}/api/posts/{post_id}", timeout=5).json()["title"] == "Дълъг бриф за събитие"


def test_timeline_shows_model_calls_retries_and_timeouts():
    # „Ход на работата": всяко извикване към модела с време, № на опита и причината за повтор.
    from src.llm.client import CallLogEntry
    from src.llm.roles import Role
    from src.web import worker

    class FakeClient:
        config = None

        def __init__(self) -> None:
            self.call_log = []
            self.http = 0

        def _post_chat(self, model, messages, temperature=None, **kwargs):
            self.http += 1
            if self.http == 1:
                raise TimeoutError("read timed out")
            return "{}"

        def complete(self, role, prompt, **kwargs):
            for _ in range(2):
                try:
                    text = self._post_chat("velocity/Qwen", [])
                    break
                except TimeoutError:
                    continue
            self.call_log.append(CallLogEntry(role=role.value, model="velocity/Qwen", duration_seconds=1, success=True, attempts=1))
            return text

    sent: list[dict] = []
    timeline = worker.Timeline(sent.append)
    client = FakeClient()
    worker.instrument(client, timeline)
    client.complete(Role.CRITIC, "провери", images=["a.jpg", "b.jpg"])

    events = [m["ev"] for m in sent]
    assert [e["type"] for e in events] == ["call", "http", "http_fail", "http", "call_end"]
    assert events[0]["role"] == "critic" and events[0]["images"] == 2
    assert events[2]["err"] == "изтече времето за отговор"
    assert events[3]["n"] == 2 and events[-1]["ok"] is True
