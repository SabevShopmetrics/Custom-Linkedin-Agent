"""
Единен HTTP клиент към Open WebUI (OpenAI-съвместим endpoint). Никакъв vendor SDK —
само `requests`. Един клас LLMClient с метод complete(role, prompt, ...), който сам
избира модела по ролята и връща или суров текст, или валидиран pydantic обект,
ако е подадена схема.

Отговорности, събрани нарочно на едно място (вижте docs/prompts/agent-bootstrap-prompt.md,
раздел "ОБРАБОТКА НА ГРЕШКИ"):
  - retry с нарастващо изчакване при мрежов/модеlен проблем, после fallback модел
  - retry с поправящ промпт при невалиден структуриран изход
  - автоматично свиване на снимки над лимита, преди да гръмне ImageTooLargeError
  - лог на всяко логическо извикване (роля, модел, време, успех, брой опити) за meta.json
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests
from pydantic import BaseModel

from src.config import Config
from src.errors import ImageTooLargeError, InvalidResponseError, ModelUnavailableError
from src.llm.roles import Role, model_for_role, supports_vision
from src.llm.schema import parse_structured

# "Expecting ',' delimiter" / "Expecting ':' delimiter" почти винаги значат неескейпната
# кавичка вътре в стойност на низ (моделът цитира фраза и забравя да екранира \") —
# открито при реално end-to-end тестване на 2026-08-10. Генеричното "невалиден JSON"
# съобщение не помага на модела да поправи точно това; конкретна диагноза помага.
_QUOTE_ESCAPE_HINT = (
    " Вероятна причина: неескейпната кавичка (\") вътре в стойност на низ (напр. цитираш "
    "фраза с \" вместо ' или без кавички). Никога не влагай \" или „...\" в JSON низ — "
    "ползвай единични кавички ' или перифразирай без кавички."
)


def _corrective_hint(error_text: str) -> str:
    if "delimiter" in error_text:
        return _QUOTE_ESCAPE_HINT
    return ""


@dataclass
class CallLogEntry:
    role: str
    model: str
    duration_seconds: float
    success: bool
    attempts: int
    used_fallback: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "duration_seconds": round(self.duration_seconds, 2),
            "success": self.success,
            "attempts": self.attempts,
            "used_fallback": self.used_fallback,
            "error": self.error,
        }


class LLMClient:
    def __init__(
        self,
        config: Config,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._sleep = sleep_fn
        self._clock = clock
        self.call_log: list[CallLogEntry] = []

    # ------------------------------------------------------------------ публично API

    def complete(
        self,
        role: Role,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        schema: type[BaseModel] | None = None,
    ) -> str | BaseModel:
        """
        Извиква модела, конфигуриран за `role`.

        Ако `schema` е подадена, отговорът се валидира и се връща инстанция на схемата;
        при невалиден изход се прави поправящ retry (максимум `invalid_response_max_retries`
        допълнителни опита), преди да гръмне InvalidResponseError.
        """
        model = model_for_role(role, self._config)
        messages = self._build_messages(role, prompt, system=system, images=images)

        started = self._clock()
        network_attempts = 0
        used_fallback = False

        try:
            if schema is None:
                text, network_attempts, used_fallback = self._call_with_network_retry(role, model, messages)
                self._log(role, model, started, success=True, attempts=network_attempts, used_fallback=used_fallback)
                return text

            # структуриран изход — валидация + поправящ retry
            attempts_used = 0
            last_error: str | None = None
            current_messages = messages
            max_correction_attempts = self._config.invalid_response_max_retries

            for correction_attempt in range(max_correction_attempts + 1):
                text, net_attempts, fb = self._call_with_network_retry(role, model, current_messages)
                network_attempts += net_attempts
                used_fallback = used_fallback or fb
                attempts_used += 1
                try:
                    parsed = parse_structured(text, schema)
                    self._log(
                        role, model, started, success=True, attempts=attempts_used, used_fallback=used_fallback
                    )
                    return parsed
                except ValueError as exc:
                    last_error = str(exc)
                    if correction_attempt < max_correction_attempts:
                        current_messages = current_messages + [
                            {"role": "assistant", "content": text},
                            {
                                "role": "user",
                                "content": (
                                    "Отговорът не е валиден JSON по исканата схема. Грешка: "
                                    f"{last_error}.{_corrective_hint(last_error)} Върни САМО валиден "
                                    "JSON обект, без обяснения и без markdown ограждане."
                                ),
                            },
                        ]

            self._log(
                role,
                model,
                started,
                success=False,
                attempts=attempts_used,
                used_fallback=used_fallback,
                error=last_error,
            )
            raise InvalidResponseError(role.value, model, attempts_used, detail=last_error)

        except ModelUnavailableError as exc:
            self._log(
                role,
                model,
                started,
                success=False,
                attempts=network_attempts or self._config.model_max_retries,
                used_fallback=used_fallback,
                error=str(exc),
            )
            raise

    def check_connection(self) -> dict[str, dict[str, Any]]:
        """
        Пробва всеки конфигуриран модел с минимален промпт. Не хвърля изключение —
        връща статус за всеки, за да може `cli check` да отпечата таблица дори при
        частичен провал.
        """
        results: dict[str, dict[str, Any]] = {}
        roles_and_models = [
            ("planner", self._config.model_planner),
            ("writer", self._config.model_writer),
            ("media", self._config.model_media),
            ("critic", self._config.model_critic),
            ("consolidator", self._config.model_consolidator),
            ("fallback", self._config.model_fallback),
        ]
        for label, model in roles_and_models:
            started = self._clock()
            try:
                self._post_chat(model, [{"role": "user", "content": "Отговори само с 'ОК'."}])
                results[label] = {"model": model, "ok": True, "duration_seconds": round(self._clock() - started, 2)}
            except Exception as exc:  # тук нарочно широко — това е диагностична команда
                results[label] = {
                    "model": model,
                    "ok": False,
                    "duration_seconds": round(self._clock() - started, 2),
                    "error": str(exc),
                }
        return results

    # ------------------------------------------------------------------ вътрешни

    def _build_messages(
        self,
        role: Role,
        prompt: str,
        *,
        system: str | None,
        images: list[Path] | None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})

        if images and supports_vision(role):
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image_path in images:
                content.append({"type": "image_url", "image_url": {"url": self._encode_image(image_path)}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": prompt})

        return messages

    _MIME_BY_SUFFIX = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jfif": "image/jpeg",  # JFIF е JPEG в друг контейнер — байтовете са същите
    }

    def _encode_image(self, image_path: Path) -> str:
        """Кодира снимка в base64 data URI, свивайки я автоматично ако е над лимита."""
        data = image_path.read_bytes()
        limit = self._config.max_image_bytes
        mime = self._MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")

        if len(data) > limit:
            data = self._shrink_image(image_path, data, limit)
            mime = "image/jpeg"  # _shrink_image винаги връща JPEG, независимо от оригиналния формат

        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    def _shrink_image(self, image_path: Path, data: bytes, limit: int) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover — зависимостта е декларирана в pyproject
            raise ImageTooLargeError(image_path.name, len(data), limit) from exc

        image = Image.open(io.BytesIO(data))
        image = image.convert("RGB")

        quality = 85
        scale = 1.0
        for _ in range(6):
            buffer = io.BytesIO()
            resized = image
            if scale < 1.0:
                new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                resized = image.resize(new_size)
            resized.save(buffer, format="JPEG", quality=quality)
            candidate = buffer.getvalue()
            if len(candidate) <= limit:
                return candidate
            quality = max(40, quality - 15)
            scale = max(0.3, scale - 0.15)

        raise ImageTooLargeError(image_path.name, len(candidate), limit)

    def _call_with_network_retry(
        self, role: Role, model: str, messages: list[dict[str, Any]]
    ) -> tuple[str, int, bool]:
        """
        Retry с нарастващо изчакване на основния модел, после един опит с fallback модела.
        Връща (текст, брой_опити, дали_е_ползван_fallback).
        """
        attempts = 0
        last_exc: Exception | None = None

        for attempt in range(self._config.model_max_retries):
            attempts += 1
            try:
                return self._post_chat(model, messages), attempts, False
            except Exception as exc:  # мрежов/модел проблем — опитваме пак
                last_exc = exc
                if attempt < self._config.model_max_retries - 1:
                    self._sleep(2**attempt)

        # основният модел е изчерпан — един опит с fallback, ако не е самият той fallback-ът
        if model != self._config.model_fallback:
            attempts += 1
            try:
                return self._post_chat(self._config.model_fallback, messages), attempts, True
            except Exception as exc:
                last_exc = exc

        raise ModelUnavailableError(role.value, model, attempts, detail=str(last_exc) if last_exc else None)

    def _post_chat(self, model: str, messages: list[dict[str, Any]]) -> str:
        response = requests.post(
            f"{self._config.openwebui_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._config.openwebui_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "messages": messages},
            timeout=self._config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Неочакван формат на отговора от Open WebUI: {payload}") from exc

    def _log(
        self,
        role: Role,
        model: str,
        started: float,
        *,
        success: bool,
        attempts: int,
        used_fallback: bool,
        error: str | None = None,
    ) -> None:
        self.call_log.append(
            CallLogEntry(
                role=role.value,
                model=model,
                duration_seconds=self._clock() - started,
                success=success,
                attempts=attempts,
                used_fallback=used_fallback,
                error=error,
            )
        )
