from src.pipeline.util import list_photos


def test_list_photos_recognizes_jfif(tmp_path):
    """.jfif е честото разширение при сваляне на снимки от браузър (Google/Bing Images) —
    без това, потребител, който сложи такава снимка в photos/, би получил тих no-photos
    път вместо ясен сигнал, че снимката не е разпозната."""
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    (photos_dir / "meeting.jfif").write_bytes(b"fake-jpeg-bytes")

    photos = list_photos(photos_dir)

    assert [p.name for p in photos] == ["meeting.jfif"]


def test_list_photos_ignores_unsupported_extensions(tmp_path):
    photos_dir = tmp_path / "photos"
    photos_dir.mkdir()
    (photos_dir / "notes.txt").write_text("not a photo")
    (photos_dir / "photo.jpg").write_bytes(b"fake")

    photos = list_photos(photos_dir)

    assert [p.name for p in photos] == ["photo.jpg"]
