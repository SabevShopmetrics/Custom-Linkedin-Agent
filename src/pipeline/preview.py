"""
Preview — самостоятелен изглед на готов пост (текст + избрани снимки), какъвто ще изглежда
в LinkedIn. НЕ е стъпка от конвейера (run_pipeline не я вика) — чисто dev/QA инструмент за
`python -m src.cli preview <post_dir>`, след като output/ вече съществува.

Самостоятелен HTML файл (снимките са base64-вградени), за да се отваря директно с
file:// без нужда от локален сървър.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from src.humanize import humanize_notes, status_label
from src.pipeline.util import read_text_or_none

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jfif": "image/jpeg",
}

_TEMPLATE = """\
<!doctype html>
<html lang="bg">
<head>
<meta charset="utf-8">
<title>Преглед — {title}</title>
<style>
  body {{
    margin: 0; padding: 32px 16px; background: #f3f2ef;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  .card {{
    max-width: 560px; margin: 0 auto 24px; background: #fff; border-radius: 8px;
    box-shadow: 0 0 0 1px rgba(0,0,0,.08); overflow: hidden;
  }}
  .card-head {{ display: flex; align-items: center; gap: 12px; padding: 16px; }}
  .avatar {{
    width: 48px; height: 48px; border-radius: 50%; background: #0a66c2; color: #fff;
    display: flex; align-items: center; justify-content: center; font-weight: 700; flex: none;
  }}
  .who b {{ display: block; font-size: 14px; }}
  .who span {{ font-size: 12px; color: #666; }}
  .post-text {{ padding: 0 16px 12px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; }}
  .photos {{ display: grid; gap: 2px; }}
  .photos.n1 {{ grid-template-columns: 1fr; }}
  .photos.n2 {{ grid-template-columns: 1fr 1fr; }}
  .photos.n3 {{ grid-template-columns: 1fr 1fr; }}
  .photos.n3 img:first-child {{ grid-column: span 2; }}
  .photos.n4, .photos.n5 {{ grid-template-columns: 1fr 1fr; }}
  .photos img {{ width: 100%; height: 260px; object-fit: cover; display: block; }}
  .meta-bar {{ display: flex; gap: 16px; padding: 10px 16px; border-top: 1px solid #eee; color: #666; font-size: 13px; }}
  h1 {{ max-width: 560px; margin: 0 auto 16px; font-size: 18px; }}
  .status {{
    max-width: 560px; margin: 0 auto 16px; padding: 14px 18px; border-radius: 6px;
    font-size: 15px; font-weight: 600; display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap;
  }}
  .status.ready {{ background: #e7f6ec; color: #146c2e; border: 1px solid #b7e4c7; }}
  .status.ready_with_notes {{ background: #fff6e0; color: #8a5a00; border: 1px solid #f2d68a; }}
  .copy-btn {{
    font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
    border: 1px solid #0a66c2; background: #0a66c2; color: #fff; border-radius: 20px;
    padding: 8px 18px; white-space: nowrap;
  }}
  .copy-btn:hover {{ background: #004182; border-color: #004182; }}
  .copy-btn.copied {{ background: #146c2e; border-color: #146c2e; }}
  .notes {{
    max-width: 560px; margin: 0 auto 24px; font-size: 13px; color: #444; background: #fff6e0;
    border: 1px solid #f2d68a; border-radius: 6px; padding: 14px 18px 14px 34px;
  }}
  .notes-title {{ font-weight: 700; color: #8a5a00; margin: 0 0 8px -18px; font-size: 13px; }}
  .notes li {{ margin-bottom: 6px; }}
</style>
</head>
<body>
  <h1>Преглед на пост — {title}</h1>
  <div class="status {status_class}">
    <span><b>{status}</b>{notes_count}</span>
    <button class="copy-btn" id="copy-btn" onclick="copyPostText()">📋 Копирай текста</button>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="avatar">ИКТ</div>
      <div class="who">
        <b>ИКТ Клъстер – Варна</b>
        <span>сега · 🌐</span>
      </div>
    </div>
    <div class="post-text" id="post-text">{post_html}</div>
    {photos_html}
    <div class="meta-bar">👍 Харесай &nbsp; 💬 Коментирай &nbsp; ↗ Сподели</div>
  </div>

  {notes_html}

  <script>
    function copyPostText() {{
      var el = document.getElementById('post-text');
      var text = el.innerText || el.textContent;
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      var ok = false;
      try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
      document.body.removeChild(ta);
      var btn = document.getElementById('copy-btn');
      if (ok) {{
        var original = btn.textContent;
        btn.textContent = '✓ Копирано!';
        btn.classList.add('copied');
        setTimeout(function () {{ btn.textContent = original; btn.classList.remove('copied'); }}, 1500);
      }} else {{
        alert('Копирането не успя автоматично — маркирай текста на поста ръчно.');
      }}
    }}
  </script>
</body>
</html>
"""


def _photo_data_uri(path: Path) -> str:
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def render_preview(post_dir: Path) -> Path:
    """
    Чете output/post.md + output/meta.json + output/selected/ на вече завършен пост и
    записва output/preview.html — самостоятелен файл, отворим директно в браузър.
    """
    output_dir = post_dir / "output"
    post_text = read_text_or_none(output_dir / "post.md")
    if post_text is None:
        raise FileNotFoundError(f"Няма {output_dir / 'post.md'} — пусни целия конвейер първо.")

    meta_raw = read_text_or_none(output_dir / "meta.json")
    meta = json.loads(meta_raw) if meta_raw else {}

    selected_dir = output_dir / "selected"
    photo_paths = sorted(selected_dir.iterdir()) if selected_dir.is_dir() else []

    photos_html = ""
    if photo_paths:
        n = min(len(photo_paths), 5)
        imgs = "".join(f'<img src="{_photo_data_uri(p)}" alt="{html.escape(p.name)}">' for p in photo_paths[:5])
        photos_html = f'<div class="photos n{n}">{imgs}</div>'

    # research-plan-36, В6: бележките на човешки език (както в терминала) — суровите остават
    # в meta.json. „Спонсорирано" е махнато: постовете на клъстера не са реклама.
    notes = humanize_notes(meta.get("open_notes", []) + meta.get("warnings", []))
    notes_items = "".join(f"<li>{html.escape(n)}</li>" for n in notes)
    notes_html = (
        f"<div class='notes'><p class='notes-title'>⚠ Проверете преди публикуване:</p>"
        f"<ul>{notes_items}</ul></div>"
        if notes_items
        else ""
    )

    status = meta.get("status", "?")
    final_check = (meta.get("final_check") or {}).get("status")
    pending = " · окончателната проверка още тече — презаредете след минута" if final_check == "pending" else ""
    rendered = _TEMPLATE.format(
        title=html.escape(post_dir.name),
        post_html=html.escape(post_text.strip()),
        photos_html=photos_html,
        status=html.escape(status_label(status)),
        notes_count=html.escape((f" · {len(notes)} бележки" if notes else "") + pending),
        status_class=status if status in ("ready", "ready_with_notes") else "ready_with_notes",
        notes_html=notes_html,
    )

    preview_path = output_dir / "preview.html"
    preview_path.write_text(rendered, encoding="utf-8")
    return preview_path
