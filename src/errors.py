"""
Типове грешки за LinkedIn агента.

Всяка грешка носи `human_message` — изречение, което казва на човека какво точно
да провери или направи. Никога не подаваме на потребителя суров traceback или
HTTP код като единствено обяснение.

Йерархия:
    AgentError                     — базов клас, всички грешки на агента наследяват от него
    ├── BriefingMissingError       — спира конвейера, преди да е повикан какъвто и да е модел
    ├── BriefingNotFilledError     — briefing.md е нередактиран шаблон, спира преди LLM извиквания
    ├── ModelUnavailableError      — модел не отговаря след retry + fallback
    ├── InvalidResponseError       — модел върна невалиден структуриран изход
    ├── ImageTooLargeError         — снимка не може да се свие под лимита
    └── NeedsHumanReviewError      — Критикът/GDPR правилата спряха конвейера, needs_human

`NoPhotosWarning` не е грешка (не наследява AgentError) — тя е сигнал, който се
логва и се записва в meta.json, но никога не спира конвейера.
"""

from __future__ import annotations


class AgentError(Exception):
    """Базова грешка на агента. Винаги носи човешко обяснение."""

    def __init__(self, human_message: str, *, detail: str | None = None) -> None:
        self.human_message = human_message
        self.detail = detail
        super().__init__(human_message if not detail else f"{human_message}\n({detail})")


class BriefingMissingError(AgentError):
    """Няма briefing.md или е празен. Спира конвейера веднага — преди LLM извиквания."""

    def __init__(self, post_dir: str) -> None:
        super().__init__(
            f"Липсва или е празен briefing.md в '{post_dir}'. "
            f"Създайте файла и опишете повода за поста, преди да пуснете генерирането отново."
        )
        self.post_dir = post_dir


class BriefingNotFilledError(AgentError):
    """
    briefing.md е точно копие на нередактирания шаблон от posts/_example/ — типично след
    `new <име>` без последващо редактиране. Спира преди LLM извикванията: без това, Планер
    → Писател → Критик → Ревизия → Критик пак се изпълняват срещу празен бриф на цената на
    реални модели (наблюдавано 2026-08-10 — 5 извиквания, ~5 минути, за да стигне до
    същия извод, който тук се хваща безплатно).
    """

    def __init__(self, post_dir: str) -> None:
        super().__init__(
            f"briefing.md в '{post_dir}' изглежда е все още нередактираният шаблон от "
            f"posts/_example/. Попълнете реални данни за повода (кой, какво, кога, къде), "
            f"преди да пуснете генерирането."
        )
        self.post_dir = post_dir


class ModelUnavailableError(AgentError):
    """Моделът не отговори след retry-и, и fallback моделът също не помогна."""

    def __init__(self, role: str, model: str, attempts: int, *, detail: str | None = None) -> None:
        super().__init__(
            f"Моделът '{model}' (роля: {role}) не отговори след {attempts} опита "
            f"и fallback моделът също не помогна. Проверете дали моделът е достъпен в Open WebUI "
            f"и дали API ключът в .env е валиден.",
            detail=detail,
        )
        self.role = role
        self.model = model
        self.attempts = attempts


class InvalidResponseError(AgentError):
    """Моделът върна изход, който не минава валидацията по схема, дори след поправящ промпт."""

    def __init__(self, role: str, model: str, attempts: int, *, detail: str | None = None) -> None:
        super().__init__(
            f"Моделът '{model}' (роля: {role}) върна невалиден структуриран изход "
            f"{attempts} пъти подред — вероятно не следва изисквания JSON формат. "
            f"Проверете промпта за тази роля или пробвайте друг модел за нея.",
            detail=detail,
        )
        self.role = role
        self.model = model
        self.attempts = attempts


class ImageTooLargeError(AgentError):
    """Снимка остава над лимита дори след автоматично свиване."""

    def __init__(self, filename: str, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            f"Снимката '{filename}' е {size_bytes // 1024} KB дори след автоматично свиване "
            f"(лимит {limit_bytes // 1024} KB). Свалете разделителната способност ръчно и опитайте отново."
        )
        self.filename = filename
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


class NeedsHumanReviewError(AgentError):
    """
    Конвейерът спря и статусът е 'needs_human' — нищо не се трие, всичко натрупано
    остава на диска. Задейства се от два различни спусъка (виж src/pipeline/runner.py).

    От 2026-08-10 (потребителска инструкция): GDPR находки (споменаване на организация
    без mention_allowed, лично име извън брифа, снимка с вероятно непълнолетни) вече НЕ
    спират конвейера — само предупреждения в meta.json/run.log. Виж
    docs/prompts/followup-prompt-3-gdpr-warnings-and-logging.md.

      - hard_reject от Критика (нарушение на rules/forbidden.md — политическа неутралност,
        критика на партньори и т.н.) — веднага, без опит за пренаписване (immediate=True)
      - Критикът все още не одобрява текста след всички опити за ревизия
        (immediate=False)
    """

    def __init__(self, post_dir: str, reasons: list[str], *, immediate: bool) -> None:
        reasons_text = "; ".join(reasons) if reasons else "(без подробности)"
        if immediate:
            headline = (
                f"Постът в '{post_dir}' спря веднага — засегнато е правило от rules/forbidden.md, "
                f"без опит за автоматично пренаписване."
            )
        else:
            headline = f"Постът в '{post_dir}' беше отхвърлен от Критика и след всички опити за ревизия."
        super().__init__(f"{headline} Статусът е 'needs_human'. Причини: {reasons_text}")
        self.post_dir = post_dir
        self.reasons = reasons
        self.immediate = immediate


class NoPhotosWarning:
    """
    Не е грешка — папката photos/ е празна. Конвейерът продължава само с текст.

    Използва се за логване и за отбелязване в meta.json, не се хвърля (raise) като
    изключение при нормален поток.
    """

    def __init__(self, post_dir: str) -> None:
        self.post_dir = post_dir
        self.human_message = (
            f"Папката 'photos/' в '{post_dir}' е празна — постът ще съдържа само текст, без снимки."
        )

    def __str__(self) -> str:  # pragma: no cover — тривиално
        return self.human_message
