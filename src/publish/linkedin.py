"""
Publisher — готовият пост към LinkedIn страницата на клъстера (docs/technical/web-ui-2.md).

Два режима, избрани с конфигурация, не с код (prd-phase2: „draft режим сега, реален LinkedIn
API щом достъпът се одобри — смяна на конфигурация, не код"):

  PUBLISH_MODE=draft     (подразбиране) нищо не се изпраща. Пълната заявка, която би отишла
                         към LinkedIn, се записва в output/linkedin-request.json — може да се
                         прегледа, а текстът се копира и публикува ръчно.
  PUBLISH_MODE=linkedin  истинско публикуване през Community Management API (Posts API):
                         LINKEDIN_ACCESS_TOKEN (w_organization_social), LINKEDIN_ORG_ID
                         (числото от urn:li:organization:<id>), LINKEDIN_VERSION (YYYYMM).

ВАЖНО: достъпът до API още не е одобрен (CLAUDE.md, план 9) — режимът `linkedin` е написан
по документацията и НЕ е пускан срещу истинския LinkedIn. Виж „Недоказани твърдения" в
web-ui-2.md. Публикуването е винаги изрично действие на човек (бутон + потвърждение) —
агентът никога не публикува сам.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.errors import AgentError

API_BASE = "https://api.linkedin.com/rest"
DEFAULT_VERSION = "202509"
MAX_IMAGES = 20  # LinkedIn приема до 20 снимки в multiImage пост
REQUEST_FILE = "linkedin-request.json"
RESULT_FILE = "publish.json"


class PublishError(AgentError):
    """Публикуването не мина — съобщението казва на човека какво да провери."""


@dataclass(frozen=True)
class PublishSettings:
    mode: str = "draft"  # "draft" | "linkedin"
    access_token: str = ""
    org_id: str = ""
    version: str = DEFAULT_VERSION
    timeout: int = 60

    @property
    def author(self) -> str:
        return f"urn:li:organization:{self.org_id}" if self.org_id else "urn:li:organization:<LINKEDIN_ORG_ID>"

    def problem(self) -> str | None:
        """Какво липсва за реално публикуване (None — всичко е налице или режимът е draft)."""
        if self.mode != "linkedin":
            return None
        missing = [
            name
            for name, value in (("LINKEDIN_ACCESS_TOKEN", self.access_token), ("LINKEDIN_ORG_ID", self.org_id))
            if not value
        ]
        if missing:
            return "За публикуване в LinkedIn липсват настройки в .env: " + ", ".join(missing) + "."
        if not self.org_id.isdigit():
            return "LINKEDIN_ORG_ID трябва да е само числото на страницата (от urn:li:organization:<число>)."
        return None


def settings_from_env() -> PublishSettings:
    mode = os.environ.get("PUBLISH_MODE", "").strip().lower()
    return PublishSettings(
        mode="linkedin" if mode == "linkedin" else "draft",
        access_token=os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip(),
        org_id=os.environ.get("LINKEDIN_ORG_ID", "").strip(),
        version=os.environ.get("LINKEDIN_VERSION", "").strip() or DEFAULT_VERSION,
    )


@dataclass
class PublishResult:
    mode: str  # "draft" | "linkedin"
    post_urn: str | None = None
    url: str | None = None
    request_file: str | None = None
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "post_urn": self.post_urn,
            "url": self.url,
            "request_file": self.request_file,
            "images": self.images,
        }


# --------------------------------------------------------------------------- текст

# Posts API приема „little text format": тези знаци имат значение (споменавания, хаштагове,
# форматиране) и трябва да се екранират, иначе част от текста изчезва или заявката се отказва.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def escape_commentary(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)


def build_post_payload(settings: PublishSettings, text: str, image_urns: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "author": settings.author,
        "commentary": escape_commentary(text.strip()),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if len(image_urns) == 1:
        payload["content"] = {"media": {"id": image_urns[0]}}
    elif image_urns:
        payload["content"] = {"multiImage": {"images": [{"id": urn} for urn in image_urns]}}
    return payload


# --------------------------------------------------------------------------- публикуване


def publish(
    output_dir: Path,
    text: str,
    photos: list[Path],
    settings: PublishSettings | None = None,
    *,
    session: Any = None,
) -> PublishResult:
    """
    Публикува `text` (+ `photos`, в реда им) или — в режим draft — само записва заявката.
    `session` подменя requests.Session (тестове, без мрежа).
    """
    settings = settings or settings_from_env()
    text = (text or "").strip()
    if not text:
        raise PublishError("Няма текст за публикуване.")
    if len(text) > 3000:
        raise PublishError(f"Текстът е {len(text)} знака — LinkedIn приема до 3000. Съкратете го и опитайте пак.")
    photos = photos[:MAX_IMAGES]
    output_dir.mkdir(parents=True, exist_ok=True)

    if settings.mode != "linkedin":
        placeholders = [f"urn:li:image:<{p.name}>" for p in photos]
        request = {
            "mode": "draft",
            "note": "Нищо не е изпратено. Така би изглеждала заявката към LinkedIn (PUBLISH_MODE=linkedin).",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "endpoint": f"POST {API_BASE}/posts",
            "headers": {"LinkedIn-Version": settings.version, "X-Restli-Protocol-Version": "2.0.0"},
            "images": [p.name for p in photos],
            "payload": build_post_payload(settings, text, placeholders),
        }
        target = output_dir / REQUEST_FILE
        target.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return PublishResult(mode="draft", request_file=f"output/{REQUEST_FILE}", images=[p.name for p in photos])

    problem = settings.problem()
    if problem:
        raise PublishError(problem)
    if session is None:
        import requests

        session = requests.Session()
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "LinkedIn-Version": settings.version,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    image_urns = [_upload_image(session, settings, headers, photo) for photo in photos]
    payload = build_post_payload(settings, text, image_urns)
    response = _call(session, "post", f"{API_BASE}/posts", headers={**headers, "Content-Type": "application/json"},
                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=settings.timeout)
    post_urn = response.headers.get("x-restli-id") or response.headers.get("X-RestLi-Id")
    if not post_urn:
        raise PublishError(
            "LinkedIn прие заявката, но не върна адрес на поста — проверете страницата ръчно, преди да опитате пак.",
            detail=f"HTTP {response.status_code}",
        )
    result = PublishResult(
        mode="linkedin",
        post_urn=post_urn,
        url=f"https://www.linkedin.com/feed/update/{post_urn}/",
        images=[p.name for p in photos],
    )
    (output_dir / RESULT_FILE).write_text(
        json.dumps({**result.to_dict(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _upload_image(session: Any, settings: PublishSettings, headers: dict[str, str], photo: Path) -> str:
    init = _call(
        session, "post", f"{API_BASE}/images?action=initializeUpload",
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"initializeUploadRequest": {"owner": settings.author}}).encode("utf-8"),
        timeout=settings.timeout,
    )
    try:
        value = init.json()["value"]
        upload_url, image_urn = value["uploadUrl"], value["image"]
    except (ValueError, KeyError, TypeError) as exc:
        raise PublishError("LinkedIn не върна адрес за качване на снимка.", detail=str(exc)) from exc
    _call(session, "put", upload_url, headers={"Authorization": headers["Authorization"]},
          data=photo.read_bytes(), timeout=max(settings.timeout, 120))
    return image_urn


def _call(session: Any, method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = getattr(session, method)(url, **kwargs)
    except Exception as exc:  # noqa: BLE001 — мрежова грешка, таймаут
        raise PublishError("Няма връзка с LinkedIn — проверете интернета и опитайте пак.", detail=str(exc)) from exc
    status = response.status_code
    if status in (401, 403):
        raise PublishError(
            "LinkedIn отказа достъп — токенът е изтекъл или няма право w_organization_social за страницата. "
            "Поискайте нов токен от Super Admin на страницата.",
            detail=f"HTTP {status}: {response.text[:300]}",
        )
    if status == 429:
        raise PublishError("LinkedIn ограничи заявките (твърде много за кратко време). Опитайте след час.",
                           detail=response.text[:300])
    if status >= 400:
        raise PublishError(
            "LinkedIn не прие поста. Подробностите са в лога — текстът остава тук, нищо не е изгубено.",
            detail=f"HTTP {status}: {response.text[:500]}",
        )
    return response
