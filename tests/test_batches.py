"""График → много постове (src/web/schedule.py, batches.py) и Publisher (src/publish/linkedin.py). Без мрежа."""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from datetime import date

import pytest
import requests

from src.publish import linkedin
from src.web import schedule
from src.web import server as web_server

from tests.test_web import H, _fake_pipeline

# --------------------------------------------------------------------------- график


def test_csv_from_bulgarian_excel_cp1251_semicolon():
    data = "Дата;Тема;Режим;Снимки\n20.10.2026;Среща на членовете;кадър;среща\n;Ученици в три фирми;;a.jpg, b.jpg\n".encode("cp1251")
    parsed = schedule.parse_schedule("grafik.csv", data)
    assert [(r.date, r.brief, r.mode, r.photos) for r in parsed.rows] == [
        ("2026-10-20", "Среща на членовете", "kadar", ["среща"]),
        (None, "Ученици в три фирми", None, ["a.jpg", "b.jpg"]),
    ]


def test_rows_without_brief_are_skipped_with_warning():
    parsed = schedule.parse_schedule("g.csv", "Дата,Бриф\n20.10.2026,\n21.10.2026,Пост\n".encode())
    assert len(parsed.rows) == 1 and "няма бриф" in parsed.warnings[0]


def test_text_blocks_with_dates_and_inline_fields():
    text = "20.10.2026 — Среща на членовете.\nСнимки: среща\n---\n03.11 — Покана за уъркшоп.\n---\n3.5 милиона лева за проект."
    rows = schedule.parse_schedule("g.txt", text.encode()).rows
    by_brief = {r.brief: r for r in rows}
    assert by_brief["Среща на членовете."].photos == ["среща"]
    assert by_brief["Покана за уъркшоп."].date is not None
    assert by_brief["3.5 милиона лева за проект."].date is None  # число, не дата


def test_short_dates_pick_the_nearest_future():
    assert schedule.parse_date("03.11", today=date(2026, 10, 13)) == "2026-11-03"
    assert schedule.parse_date("15.01", today=date(2026, 12, 20)) == "2027-01-15"
    assert schedule.parse_date("20 октомври 2026") == "2026-10-20"
    assert schedule.parse_date(46315) == "2026-10-20"  # сериен номер от Excel
    assert schedule.parse_date("31.02.2026") is None


def _xlsx(rows: list[list[object]], date_style_col: int | None = None) -> bytes:
    shared: list[str] = []
    sheet_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, str):
                shared.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
            else:
                style = ' s="1"' if c == date_style_col else ""
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Лист1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    return buffer.getvalue()


def test_xlsx_without_libraries_reads_strings_and_date_cells():
    data = _xlsx([["Дата", "Бриф"], [46315, "Среща на членовете"], [46319, "Хакатон"]], date_style_col=0)
    rows = schedule.parse_schedule("grafik.xlsx", data).rows
    assert [(r.date, r.brief) for r in rows] == [("2026-10-20", "Среща на членовете"), ("2026-10-24", "Хакатон")]


def test_unreadable_formats_explain_what_to_do():
    with pytest.raises(schedule.ScheduleError, match="xlsx или .csv"):
        schedule.parse_schedule("grafik.xls", b"\xd0\xcf")
    with pytest.raises(schedule.ScheduleError, match="истински Excel"):
        schedule.parse_schedule("grafik.xlsx", b"not a zip")
    with pytest.raises(schedule.ScheduleError, match="нито един пост"):
        schedule.parse_schedule("g.csv", b"")


# --------------------------------------------------------------------------- пакет през HTTP


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(web_server.runner, "run_pipeline", _fake_pipeline)
    posts = tmp_path / "posts"
    posts.mkdir()
    app = web_server.WebApp(
        posts, client_factory=lambda: object(), in_process=True, max_jobs=2,
        publish_settings=linkedin.PublishSettings(mode="draft"),
    )
    httpd = web_server.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app, posts
    httpd.shutdown()
    httpd.server_close()
    app.close()


def _wait_batch(base: str, batch_id: str) -> dict:
    for _ in range(200):
        status = requests.get(f"{base}/api/batches/{batch_id}", timeout=5).json()
        if not any(i["state"] in ("running", "queued") for i in status["items"]):
            return status
        time.sleep(0.05)
    raise AssertionError("пакетът не приключи")


def test_schedule_runs_all_posts_through_the_queue(live, monkeypatch):
    base, app, posts = live
    gate = threading.Event()
    peak = []

    def slow(post_dir, client, **kwargs):
        peak.append(len(app.running_jobs()))
        gate.wait(5)
        _fake_pipeline(post_dir, client, **kwargs)

    monkeypatch.setattr(web_server.runner, "run_pipeline", slow)
    parsed = requests.post(f"{base}/api/schedule/parse?name=g.txt", data="Първи пост\n\nВтори пост\n\nТрети пост\n\nЧетвърти".encode(),
                           headers=H, timeout=5).json()
    assert len(parsed["rows"]) == 4
    created = requests.post(f"{base}/api/batches", json={"name": "Октомври", "rows": parsed["rows"], "start": True},
                            headers=H, timeout=5)
    assert created.status_code == 201
    status = created.json()
    assert status["counts"] == {"running": 2, "queued": 2}  # max_jobs=2 — останалите чакат ред
    gate.set()
    final = _wait_batch(base, status["id"])
    assert [i["state"] for i in final["items"]] == ["done"] * 4
    assert max(peak) <= 2
    assert requests.get(f"{base}/api/batches", timeout=5).json()["batches"][0]["done"] == 4
    # „Пусни всички" не пише наново готовите
    again = requests.post(f"{base}/api/batches/{status['id']}/start", json={}, headers=H, timeout=5).json()
    assert again["added"] == 0


def test_export_zip_and_result_csv(live):
    base, _, _ = live
    rows = [{"brief": "Среща", "date": "2026-10-20"}, {"brief": "Хакатон"}]
    status = requests.post(f"{base}/api/batches", json={"rows": rows, "start": True}, headers=H, timeout=5).json()
    final = _wait_batch(base, status["id"])
    names = [i["file_name"] for i in final["items"]]
    assert names[0].startswith("01 2026-10-20 Среща") and names[1].startswith("02 без-дата")
    archive = zipfile.ZipFile(io.BytesIO(requests.get(f"{base}/api/batches/{status['id']}/export.zip", timeout=5).content))
    assert set(names) <= set(archive.namelist()) and "график-резултат.csv" in archive.namelist()
    assert archive.read(names[0]).decode("utf-8").startswith("Готов пост")
    csv_text = requests.get(f"{base}/api/batches/{status['id']}/result.csv", timeout=5).content.decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Дата;Заглавие;Статус")


def test_batch_routes_need_the_interface_header_and_valid_ids(live):
    base, _, _ = live
    assert requests.post(f"{base}/api/batches", json={"rows": [{"brief": "x"}]}, timeout=5).status_code == 403
    assert requests.get(f"{base}/api/batches/../../etc", timeout=5).status_code == 404
    assert requests.get(f"{base}/api/batches/abcdefabcdef", timeout=5).status_code == 404
    assert requests.post(f"{base}/api/batches", json={"rows": []}, headers=H, timeout=5).status_code == 400


def test_publish_in_draft_mode_sends_nothing(live):
    base, _, posts = live
    status = requests.post(f"{base}/api/batches", json={"rows": [{"brief": "Среща"}], "start": True}, headers=H, timeout=5).json()
    post_id = _wait_batch(base, status["id"])["items"][0]["post_id"]
    result = requests.post(f"{base}/api/posts/{post_id}/publish", json={}, headers=H, timeout=5).json()
    assert result["mode"] == "draft" and result["request_file"] == "output/linkedin-request.json"
    request = json.loads((posts / post_id / "output" / "linkedin-request.json").read_text(encoding="utf-8"))
    assert request["payload"]["commentary"].startswith("Готов пост")
    assert not (posts / post_id / "output" / "published.md").exists()  # нищо не е публикувано


# --------------------------------------------------------------------------- Publisher


class _Resp:
    def __init__(self, status=201, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = json.dumps(body or {})

    def json(self):
        return self._body


class _FakeSession:
    def __init__(self, post_status=201):
        self.calls = []
        self.post_status = post_status

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if "initializeUpload" in url:
            n = len([c for c in self.calls if "initializeUpload" in c[1]])
            return _Resp(200, {"value": {"uploadUrl": f"https://upload/{n}", "image": f"urn:li:image:{n}"}})
        return _Resp(self.post_status, {}, {"x-restli-id": "urn:li:share:42"})

    def put(self, url, **kwargs):
        self.calls.append(("put", url, kwargs))
        return _Resp(201)


def test_linkedin_mode_uploads_images_then_posts(tmp_path):
    photos = []
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"jpeg")
        photos.append(tmp_path / name)
    settings = linkedin.PublishSettings(mode="linkedin", access_token="t", org_id="123")
    session = _FakeSession()
    result = linkedin.publish(tmp_path / "out", "Текст с #таг и (скоби)", photos, settings, session=session)
    assert result.post_urn == "urn:li:share:42" and result.url.endswith("urn:li:share:42/")
    post = json.loads(session.calls[-1][2]["data"].decode("utf-8"))
    assert post["author"] == "urn:li:organization:123"
    assert post["commentary"] == "Текст с \\#таг и \\(скоби\\)"
    assert post["content"]["multiImage"]["images"] == [{"id": "urn:li:image:1"}, {"id": "urn:li:image:2"}]
    assert session.calls[-1][2]["headers"]["LinkedIn-Version"] == linkedin.DEFAULT_VERSION
    assert (tmp_path / "out" / linkedin.RESULT_FILE).is_file()


def test_linkedin_mode_explains_missing_settings_and_auth_errors(tmp_path):
    with pytest.raises(linkedin.PublishError, match="LINKEDIN_ACCESS_TOKEN"):
        linkedin.publish(tmp_path, "текст", [], linkedin.PublishSettings(mode="linkedin", org_id="1"))
    with pytest.raises(linkedin.PublishError, match="токенът е изтекъл"):
        linkedin.publish(tmp_path, "текст", [], linkedin.PublishSettings(mode="linkedin", access_token="t", org_id="1"),
                         session=_FakeSession(post_status=401))
    with pytest.raises(linkedin.PublishError, match="3000"):
        linkedin.publish(tmp_path, "а" * 3001, [], linkedin.PublishSettings())
