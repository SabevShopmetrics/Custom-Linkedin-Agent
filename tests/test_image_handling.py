"""
Снимка над лимита се свива автоматично (не спира конвейера); ако дори след свиване
не пасва под лимита — ImageTooLargeError с ясно съобщение. Използва истински Pillow,
без mock — само конфигурираме изкуствено малък лимит, за да предизвикаме двата пътя.
"""

import random

import pytest
from PIL import Image

from src.errors import ImageTooLargeError
from src.llm.client import LLMClient
from tests.conftest import make_config


def _make_noisy_image(path, size=(400, 400)):
    """Шумна снимка — некомпресируема добре, за да наистина да е над малък лимит."""
    image = Image.new("RGB", size)
    pixels = [
        (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(size[0] * size[1])
    ]
    image.putdata(pixels)
    image.save(path, format="PNG")
    return path


def test_encode_image_shrinks_oversized_photo_under_limit(tmp_path):
    photo = _make_noisy_image(tmp_path / "photo.png")
    original_size = photo.stat().st_size
    limit = min(20_000, original_size // 2)  # достатъчно малко, за да принуди свиване

    client = LLMClient(make_config(max_image_bytes=limit))
    data_uri = client._encode_image(photo)

    assert data_uri.startswith("data:image/jpeg;base64,")  # свиването винаги дава JPEG
    import base64

    encoded = data_uri.split(",", 1)[1]
    assert len(base64.b64decode(encoded)) <= limit


def test_encode_image_raises_clear_error_when_limit_impossible(tmp_path):
    photo = _make_noisy_image(tmp_path / "photo.png")

    client = LLMClient(make_config(max_image_bytes=10))  # невъзможен лимит

    with pytest.raises(ImageTooLargeError) as exc_info:
        client._encode_image(photo)

    assert "photo.png" in exc_info.value.human_message


def test_encode_image_under_limit_is_passed_through_unchanged(tmp_path):
    image = Image.new("RGB", (10, 10), color=(200, 100, 50))
    photo = tmp_path / "small.png"
    image.save(photo, format="PNG")

    client = LLMClient(make_config(max_image_bytes=10 * 1024 * 1024))
    data_uri = client._encode_image(photo)

    assert data_uri.startswith("data:image/png;base64,")


def test_encode_image_treats_jfif_as_jpeg_mime(tmp_path):
    """.jfif е стандартното разширение, с което Chrome/Edge свалят снимки от Google/Bing
    Images — байтовете вътре са обикновен JPEG, само разширението е различно."""
    image = Image.new("RGB", (10, 10), color=(10, 20, 30))
    photo = tmp_path / "downloaded.jfif"
    image.save(photo, format="JPEG")

    client = LLMClient(make_config(max_image_bytes=10 * 1024 * 1024))
    data_uri = client._encode_image(photo)

    assert data_uri.startswith("data:image/jpeg;base64,")


def test_encode_image_uses_webp_mime_for_webp_files(tmp_path):
    image = Image.new("RGB", (10, 10), color=(10, 20, 30))
    photo = tmp_path / "photo.webp"
    image.save(photo, format="WEBP")

    client = LLMClient(make_config(max_image_bytes=10 * 1024 * 1024))
    data_uri = client._encode_image(photo)

    assert data_uri.startswith("data:image/webp;base64,")
