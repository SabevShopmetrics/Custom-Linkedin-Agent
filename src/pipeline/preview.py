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
    max-width: 560px; margin: 0 auto 16px; padding: 12px 16px; border-radius: 6px; font-size: 13px;
  }}
  .status.ready {{ background: #e7f6ec; color: #146c2e; }}
  .status.ready_with_notes {{ background: #fff6e0; color: #8a5a00; }}
  .notes {{ max-width: 560px; margin: 0 auto 24px; font-size: 13px; color: #555; }}
  .notes li {{ margin-bottom: 6px; }}
</style>
</head>
<body>
  <h1>Преглед на пост — {title}</h1>
  <div class="status {status_class}">Статус: <b>{status}</b> · {n_notes} открит(и) въпрос(а) · {n_warnings} предупреждение(я)</div>

  <div class="card">
    <div class="card-head">
      <div class="avatar">ИКТ</div>
      <div class="who">
        <b>ИКТ Клъстер – Варна</b>
        <span>Спонсорирано · 🌐</span>
      </div>
    </div>
    <div class="post-text">{post_html}</div>
    {photos_html}
    <div class="meta-bar">👍 Харесай &nbsp; 💬 Коментирай &nbsp; ↗ Сподели</div>
  </div>

  {notes_html}
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

    open_notes = meta.get("open_notes", [])
    warnings = meta.get("warnings", [])
    notes_items = "".join(f"<li>{html.escape(n)}</li>" for n in (open_notes + warnings))
    notes_html = f"<ul class='notes'>{notes_items}</ul>" if notes_items else ""

    status = meta.get("status", "?")
    rendered = _TEMPLATE.format(
        title=html.escape(post_dir.name),
        post_html=html.escape(post_text.strip()),
        photos_html=photos_html,
        status=html.escape(status),
        status_class=status if status in ("ready", "ready_with_notes") else "ready_with_notes",
        n_notes=len(open_notes),
        n_warnings=len(warnings),
        notes_html=notes_html,
    )

    preview_path = output_dir / "preview.html"
    preview_path.write_text(rendered, encoding="utf-8")
    return preview_path
