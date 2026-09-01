"""
Тестове за интерактивния режим (src/repl.py). Никакви мрежови извиквания и никакви
реални модели — конвейерът е подменен с fake, който само записва как е бил извикан.

Целта е да се провери това, което интерактивният режим добавя над `generate`:
избор на име за папката, прикачване на снимки, /revise върху същата папка с force,
и че AgentError излиза като четимо изречение, а не като traceback.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from src.errors import BriefingMissingError, ModelUnavailableError
from src.llm.schema import PlanSchema
from src.pipeline.consolidator import PipelineResult
from src.repl import (
    Ink,
    ProgressHandler,
    Repl,
    extract_inline_attachments,
    resolve_attachments,
    slugify,
    unique_post_dir,
)
from tests.conftest import make_brand_knowledge, make_config

class FakePipeline:
    """Записва извикванията и пише output/post.md, както прави истинският Консолидатор."""

    def __init__(
        self,
        *,
        status: str = "ready",
        warnings: list[str] | None = None,
        raises: Exception | None = None,
        insufficient_brief: bool = False,
    ):
        self.calls: list[dict] = []
        self.status = status
        self.warnings = warnings or []
        self.raises = raises
        self.insufficient_brief = insufficient_brief

    def __call__(self, post_dir: Path, client, *, force: bool = False, knowledge=None, **kwargs) -> PipelineResult:
        self.calls.append({"post_dir": post_dir, "force": force, "knowledge": knowledge, "mode": kwargs.get("mode")})
        if self.raises is not None:
            raise self.raises
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост.\n", encoding="utf-8")
        return PipelineResult(
            status=self.status,
            output_dir=output_dir,
            post_path=output_dir / "post.md",
            meta_path=output_dir / "meta.json",
            warnings=self.warnings,
            insufficient_brief=self.insufficient_brief,
        )

class FakeClient:
    """
    `plan_notes` подадено на конструктора се връща като `plan.notes` от `.complete()` —
    ползва се за симулиране на "тънък бриф" в тестовете за `_thin_brief_precheck`
    (research-plan-24-institution-gdpr-false-positive-and-word-floor.md, Вариант 2).
    По подразбиране е празно (не съдържа "тънък"), затова за всеки тест, който не се
    интересува от тази функционалност, precheck-ът минава транзитивно, без да пита нищо.
    """

    def __init__(self, config, *, plan_notes: str = "") -> None:
        self.config = config
        self.call_log: list = []
        self.plan_notes = plan_notes

    def check_connection(self) -> dict:
        return {"planner": {"model": "planner-model", "ok": True, "duration_seconds": 0.1}}

    def complete(self, role, prompt, *, system=None, schema=None):
        if schema is PlanSchema:
            return PlanSchema(
                post_type="test",
                format="само текст",
                tone="test",
                needs_photos=False,
                max_photo_count=0,
                is_sensitive=False,
                notes=self.plan_notes,
                insufficient_brief=False,
            )
        raise NotImplementedError("FakeClient.complete поддържа само PlanSchema в тези тестове")

def make_repl(
    tmp_path: Path, pipeline: FakePipeline, lines: list[str], *, plan_notes: str = ""
) -> tuple[Repl, list[str]]:
    output: list[str] = []
    inputs = iter(lines)

    def input_fn(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=lambda config: FakeClient(config, plan_notes=plan_notes),
        pipeline_fn=pipeline,
        today=date(2026, 8, 11),
    )
    return repl, output

# --------------------------------------------------------------------------- имена

def test_slugify_transliterates_cyrillic():
    assert slugify("Стажантска програма 2026 в клъстера") == "stazhantska-programa-2026-v-klastera"

def test_slugify_falls_back_when_nothing_usable():
    assert slugify("!!! ???") == "post"

def test_unique_post_dir_suffixes_on_collision(tmp_path):
    posts = tmp_path / "posts"
    first = unique_post_dir(posts, "praktiki", today=date(2026, 8, 11))
    first.mkdir(parents=True)
    second = unique_post_dir(posts, "praktiki", today=date(2026, 8, 11))

    assert first.name == "2026-08-11-praktiki"
    assert second.name == "2026-08-11-praktiki-2"

# --------------------------------------------------------------------------- снимки

def test_resolve_attachments_expands_dir_and_skips_non_photos(tmp_path):
    photos = tmp_path / "снимки"
    photos.mkdir()
    (photos / "a.jpg").write_bytes(b"x")
    (photos / "b.png").write_bytes(b"x")
    (photos / "readme.txt").write_text("не е снимка", encoding="utf-8")

    found, unresolved = resolve_attachments([str(photos), str(tmp_path / "няма.jpg")])

    assert [p.name for p in found] == ["a.jpg", "b.png"]
    assert unresolved == [str(tmp_path / "няма.jpg")]

def test_extract_inline_attachments_keeps_unresolvable_token_as_text(tmp_path):
    photo = tmp_path / "екип.jpg"
    photo.write_bytes(b"x")

    text, found, unresolved = extract_inline_attachments(f'Пост за екипа @"{photo}" с благодарност @всички')

    assert found == [photo]
    assert "@всички" in text  # @споменаване в бриф не бива да се яде
    assert str(photo) not in text
    assert unresolved == ["всички"]

# --------------------------------------------------------------------------- поток

def test_brief_creates_post_dir_and_runs_pipeline(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["Стажантска програма 2026"])

    assert repl.run() == 0

    assert len(pipeline.calls) == 1
    post_dir = pipeline.calls[0]["post_dir"]
    assert post_dir.name == "2026-08-11-stazhantska-programa-2026"
    assert (post_dir / "briefing.md").read_text(encoding="utf-8").strip() == "Стажантска програма 2026"
    assert pipeline.calls[0]["force"] is False
    assert "Готов пост." in "\n".join(output)

def test_knowledge_is_loaded_once_and_passed_to_every_run(tmp_path):
    """Ефективността на интерактивния режим е точно в това — knowledge/ не се чете наново."""
    loads = []

    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Първи пост", "Втори пост"])

    def counting_loader():
        loads.append(1)
        return make_brand_knowledge()

    repl.knowledge_loader = counting_loader
    repl.run()

    assert len(loads) == 1
    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["knowledge"] is pipeline.calls[1]["knowledge"]

def test_inline_photo_is_copied_into_post_photos(tmp_path):
    photo = tmp_path / "екип.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f'Пост за екипа @"{photo}"'])

    repl.run()

    post_dir = pipeline.calls[0]["post_dir"]
    assert (post_dir / "photos" / "екип.jpg").exists()
    assert "@" not in (post_dir / "briefing.md").read_text(encoding="utf-8")

def test_attach_then_brief_uses_attachments_and_clears_them(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f"/attach {photo}", "Първи пост", "Втори пост"])

    repl.run()

    first_dir, second_dir = pipeline.calls[0]["post_dir"], pipeline.calls[1]["post_dir"]
    assert (first_dir / "photos" / "a.jpg").exists()
    assert list((second_dir / "photos").iterdir()) == []  # прикачените не се пренасят към следващия пост

def test_revise_appends_to_same_briefing_and_forces_rerun(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Стажантска програма", "/revise по-кратко, без клишета"])

    repl.run()

    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["post_dir"] == pipeline.calls[1]["post_dir"]  # същата папка, не нов пост
    assert pipeline.calls[1]["force"] is True  # брифът се промени → планът също трябва
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert "Бележка за ревизия 1" in briefing
    assert "по-кратко, без клишета" in briefing

def test_revise_instruction_overrides_sticky_mode_for_that_run_only(tmp_path):
    """
    Регресионен тест за реален случай от живо тестване (2026-08-26): "Панорама" активна,
    /revise поиска "по-кратко" — трябва да генерира с по-къс ефективен режим ЗА ТАЗИ
    ревизия, докато sticky режимът на сесията остава "Панорама" за следващия НОВ пост.
    """
    pipeline = FakePipeline()
    repl, output = make_repl(
        tmp_path,
        pipeline,
        ["/mode панорама", "Годишнина на клъстера", "/revise да звучи по-кратко", "Нов повод"],
    )

    repl.run()

    assert len(pipeline.calls) == 3
    assert pipeline.calls[0]["mode"] == "panorama"  # първият пост — sticky режимът
    assert pipeline.calls[1]["mode"] == "fokus"  # ревизията — едно стъпало надолу, само тук
    assert pipeline.calls[2]["mode"] == "panorama"  # следващият НОВ пост — sticky остава непипнат
    assert any("Фокус само за тази ревизия" in line for line in output)


def test_revise_instruction_without_length_signal_keeps_sticky_mode(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(
        tmp_path, pipeline, ["/mode панорама", "Годишнина на клъстера", "/revise звучи по-топло"]
    )

    repl.run()

    assert pipeline.calls[1]["mode"] == "panorama"  # без сигнал за дължина — режимът не се пипа


def test_revise_without_post_does_not_call_pipeline(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/revise нещо"])

    repl.run()

    assert pipeline.calls == []
    assert "напиши бриф" in "\n".join(output)

def test_again_reruns_same_dir_with_force(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Стажантска програма", "/again"])

    repl.run()

    assert pipeline.calls[1]["post_dir"] == pipeline.calls[0]["post_dir"]
    assert pipeline.calls[1]["force"] is True

def test_agent_error_is_shown_as_sentence_not_traceback(tmp_path):
    pipeline = FakePipeline(raises=BriefingMissingError("posts/x"))
    repl, output = make_repl(tmp_path, pipeline, ["Някакъв бриф"])

    assert repl.run() == 0  # грешката в един пост не сваля сесията

    text = "\n".join(output)
    assert "Спряно:" in text
    assert "briefing.md" in text
    assert "Traceback" not in text

def test_insufficient_brief_prompt_is_skipped_for_a_normal_result(tmp_path):
    """
    B4 (виж research-plan-18-critique-of-plan-17.md, А2) не се задейства без флага — нормален
    резултат минава без допълнителен input_fn въпрос (иначе входният ред би се изчерпал по-рано
    и repl.run() би паднал с EOFError извън контролирания край на сесията).
    """
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["Нормален бриф за пост."])

    assert repl.run() == 0

    assert len(pipeline.calls) == 1

def test_insufficient_brief_prompt_can_be_declined(tmp_path):
    """Enter (празен отговор) приема готовото предложение без ново пускане."""
    pipeline = FakePipeline(insufficient_brief=True)
    repl, output = make_repl(tmp_path, pipeline, ["бриф без съдържание", ""])

    assert repl.run() == 0

    assert len(pipeline.calls) == 1

def test_insufficient_brief_prompt_appends_detail_and_regenerates(tmp_path):
    """Непразен отговор се добавя към briefing.md и пуска конвейера наново с force=True."""
    pipeline = FakePipeline(insufficient_brief=True)
    repl, output = make_repl(tmp_path, pipeline, ["бриф без съдържание", "детайл за поста", ""])

    assert repl.run() == 0

    assert len(pipeline.calls) == 2
    assert pipeline.calls[0]["force"] is False
    assert pipeline.calls[1]["force"] is True
    assert pipeline.calls[1]["post_dir"] == pipeline.calls[0]["post_dir"]
    briefing = (pipeline.calls[1]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert "детайл за поста" in briefing

def test_text_from_an_interrupted_run_is_still_shown(tmp_path):
    """
    Ако конвейерът се счупи по средата, но на диска вече има текст, той се показва —
    по-полезно е от чисто съобщение за грешка. Маркира се като непълен, за да не се
    сбърка с финалното предложение.
    """

    def pipeline_that_writes_then_breaks(post_dir: Path, client, *, force=False, knowledge=None, **kwargs):
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Недовършен текст.\n", encoding="utf-8")
        raise ModelUnavailableError("consolidator", "някакъв-модел", 3)

    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.pipeline_fn = pipeline_that_writes_then_breaks
    repl.run()

    text = "\n".join(output)
    assert "Недовършен текст." in text
    assert "прекъснато пускане" in text
    assert "Спряно:" in text

def test_unexpected_error_does_not_end_the_session(tmp_path):
    pipeline = FakePipeline(raises=OSError("диск пълен"))
    repl, output = make_repl(tmp_path, pipeline, ["Първи бриф", "Втори бриф"])

    assert repl.run() == 0

    text = "\n".join(output)
    assert "Неочаквана грешка: OSError" in text
    assert len(pipeline.calls) == 2  # сесията продължава след счупения пост

def test_warnings_are_printed_after_the_post(tmp_path):
    pipeline = FakePipeline(warnings=["Снимка с вероятно непълнолетни лица."])
    repl, output = make_repl(tmp_path, pipeline, ["Пост за лагера"])

    repl.run()

    text = "\n".join(output)
    assert "Снимка с вероятно непълнолетни лица." in text
    assert text.index("Готов пост.") < text.index("Снимка с вероятно непълнолетни")

def test_backslash_continues_the_brief_on_next_line(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Първа част \\", "втора част"])

    repl.run()

    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Първа част\nвтора част"

def test_unknown_command_does_not_become_a_brief(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/несъществуваща"])

    repl.run()

    assert pipeline.calls == []
    assert "Непозната команда" in "\n".join(output)

def test_mode_defaults_to_fokus_and_is_passed_to_the_pipeline(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Практики за студенти"])

    repl.run()

    assert pipeline.calls[0]["mode"] == "fokus"


def test_mode_command_switches_mode_for_the_next_post(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/mode панорама", "Практики за студенти"])

    repl.run()

    assert any("Панорама" in line for line in output)
    assert pipeline.calls[0]["mode"] == "panorama"


def test_mode_command_without_argument_shows_current_choice(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/mode"])

    repl.run()

    assert any("Текущ режим" in line and "Фокус" in line for line in output)
    assert pipeline.calls == []


def test_mode_command_rejects_unknown_mode(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/mode нещо-си"])

    repl.run()

    assert any("Непознат режим" in line for line in output)
    assert pipeline.calls == []


def test_bare_keyword_opens_the_wizard_without_a_slash(tmp_path):
    """
    Потребителска заявка 2026-08-12: /wizard трябва да се отваря и с кратка ключова дума,
    не само през слаш-команда, за човек, който не се сеща за командния синтаксис.
    """
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, [f"/attach {photo}", "нов", "Проведохме среща"])

    repl.run()

    assert any("Нов пост" in line for line in output)
    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Проведохме среща"


def test_bare_wizard_word_also_opens_the_wizard(tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f"/attach {photo}", "wizard", "Проведохме среща"])

    repl.run()

    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Проведохме среща"


def test_a_random_one_word_brief_is_not_mistaken_for_the_wizard_keyword(tmp_path):
    """Само точните ключови думи ('нов', 'wizard') отварят уизарда — не всеки кратък бриф."""
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Практики"])

    repl.run()

    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Практики"


def test_a_real_brief_starting_with_the_keyword_word_is_not_hijacked(tmp_path):
    """Точно съвпадение на целия ред, не префикс — 'Нов член...' е истински бриф, не команда."""
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["Нов член се присъедини към клъстера"])

    repl.run()

    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Нов член се присъедини към клъстера"


def test_exit_stops_the_loop_before_remaining_input(tmp_path):
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["/exit", "Този бриф не бива да се изпълни"])

    repl.run()

    assert pipeline.calls == []

def test_config_error_exits_with_code_1(tmp_path):
    from src.config import ConfigError

    def failing_loader():
        raise ConfigError("Липсват задължителни настройки в .env: OPENWEBUI_API_KEY")

    output: list[str] = []
    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=lambda _prompt: "",
        ink=Ink(enabled=False),
        config_loader=failing_loader,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=FakePipeline(),
    )

    assert repl.run() == 1
    assert "OPENWEBUI_API_KEY" in "\n".join(output)

def test_progress_lines_reach_the_console_during_a_run(tmp_path):
    """
    Persistent handler-ът трябва да преживее setup_run_logger, който чисти handlers на
    всяко пускане — иначе прогресът се вижда само в run.log, не и в терминала.
    """
    from src.logging_utils import setup_run_logger

    def pipeline_with_logging(post_dir: Path, client, *, force=False, knowledge=None, **kwargs):
        log = setup_run_logger(post_dir)  # точно както прави истинският runner
        log.info("[1] Планер...")
        log.warning("Папката photos/ е празна — постът ще съдържа само текст.")
        log.info("=== този ред е шум и не бива да излиза ===")
        log.info("Финален статус: ready (0 предупреждения)")  # резюмето след поста го казва
        output_dir = post_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "post.md").write_text("Готов пост.\n", encoding="utf-8")
        return PipelineResult(
            status="ready",
            output_dir=output_dir,
            post_path=output_dir / "post.md",
            meta_path=output_dir / "meta.json",
        )

    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.pipeline_fn = pipeline_with_logging
    repl.run()

    text = "\n".join(output)
    assert "[1] Планер..." in text
    assert "Папката photos/ е празна" in text
    assert "този ред е шум" not in text
    assert "Финален статус" not in text


def _emit_via_handler(handler: ProgressHandler, *, level: int, message: str) -> None:
    record = logging.LogRecord("test", level, __file__, 0, message, None, None)
    handler.emit(record)


def test_progress_handler_groups_lines_under_a_bold_step_header(tmp_path):
    """
    2026-08-12: логовете вече се групират по стъпка, не са плосък поток — нов таг
    ("[4]") отваря удебелена секция, детайли под него (със или без собствен таг) висят
    отдолу с отстъп, докато не тръгне следваща стъпка.
    """
    output: list[str] = []
    handler = ProgressHandler(output.append, Ink(enabled=False))

    _emit_via_handler(handler, level=logging.INFO, message="[4] Критик върху Post I...")
    _emit_via_handler(handler, level=logging.INFO, message="[4] присъда: needs_revision · 1 находки за запълване")
    _emit_via_handler(handler, level=logging.INFO, message="[4] находка: GDPR — махни това от текста")
    _emit_via_handler(handler, level=logging.INFO, message="[5] Писател → Post II (запълва 1 находки)...")

    assert output == [
        "",
        "▸ [4] Критик върху Post I...",
        "    · присъда: needs_revision · 1 находки за запълване",
        "    · находка: GDPR — махни това от текста",
        "",
        "▸ [5] Писател → Post II (запълва 1 находки)...",
    ]


def test_progress_handler_marks_warning_lines_with_a_different_bullet(tmp_path):
    output: list[str] = []
    handler = ProgressHandler(output.append, Ink(enabled=False))

    _emit_via_handler(handler, level=logging.INFO, message="[6б] Механични правила...")
    _emit_via_handler(
        handler, level=logging.WARNING, message="[6б] Правило от корпуса, което кодът не може да наложи: X"
    )

    assert output[-1] == "    ⚠ Правило от корпуса, което кодът не може да наложи: X"


def test_progress_handler_still_suppresses_pure_noise_lines(tmp_path):
    output: list[str] = []
    handler = ProgressHandler(output.append, Ink(enabled=False))

    _emit_via_handler(handler, level=logging.INFO, message="=== Ново пускане ===")
    _emit_via_handler(handler, level=logging.WARNING, message="Открит въпрос: нещо")

    assert output == []

def test_persistent_handler_is_removed_after_the_session(tmp_path):
    """Сесията не бива да остави handler зад себе си — иначе `generate` в същия процес
    би печатал двойно."""
    import logging

    from src.logging_utils import LOGGER_NAME

    repl, _ = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.run()

    handlers = logging.getLogger(LOGGER_NAME).handlers
    assert not any(type(h).__name__ == "ProgressHandler" for h in handlers)


# --------------------------------------------------------------------------- Ниво 0 (по-лесна употреба)
# https://github.com — предложение от 2026-08-12: /wizard за нетехнически човек, снимки
# без синтаксис, автоматично копиране в клипборда, еднократен .env bootstrap.


def test_wizard_with_inline_brief_and_existing_attachment_asks_nothing(tmp_path):
    """
    Основната промяна от потребителска обратна връзка 2026-08-12: /wizard разпитваше за
    повод/причина/кого-да-спомене/чувствителност/бележки едно по едно, и на практика не
    се различаваше от обикновен чат промпт с формуляр отгоре. Сега '/wizard <бриф>' с
    вече налична снимка не пита нищо — точно като свободен ред. Ако беше попитал нещо,
    input_fn нямаше повече редове и run() щеше да свърши с EOFError преди generate.
    """
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f"/attach {photo}", "/wizard Проведохме среща на 5 август"])

    repl.run()

    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Проведохме среща на 5 август"


def test_wizard_without_argument_asks_what_happened_then_photos(tmp_path):
    """Без снимки вече налични, /wizard пита две неща: повода, после снимки (Enter да пропусне)."""
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["/wizard", "Нещо се случи", ""])

    repl.run()

    assert len(pipeline.calls) == 1
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Нещо се случи"


def test_wizard_requires_a_non_empty_brief(tmp_path):
    """Без повод няма за какво агентът да пише — не се създава папка."""
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/wizard", ""])

    repl.run()

    assert pipeline.calls == []
    assert any("празен" in line for line in output)


def test_wizard_asks_for_photos_only_when_none_are_already_provided(tmp_path):
    """
    Единственото, което /wizard пита — снимки, и то само защото файлове по диска
    наистина не могат да се познаят. Ако вече ги няма никакви, пита; ако вече има
    (от /attach или @ в самия бриф), не пита втори път.
    """
    photo = tmp_path / "снимка.jpg"
    photo.write_bytes(b"fake")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, ["/wizard Нещо се случи", str(photo)])

    repl.run()

    post_dir = pipeline.calls[0]["post_dir"]
    assert (post_dir / "photos" / "снимка.jpg").exists()


def test_wizard_reports_an_unresolvable_photo_path(tmp_path):
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["/wizard Нещо се случи", str(tmp_path / "няма.jpg")])

    repl.run()

    assert any("не намерена снимка" in line for line in output)


def test_wizard_keeps_photos_already_staged_with_attach_and_skips_the_photo_question(tmp_path):
    """
    Бъг, поправен 2026-08-12: /wizard изхвърляше тихо снимки, вече прикачени с /attach
    преди уизарда, и никога не изчистваше self.state.attachments след себе си. Сега,
    като допълнение, наличието им означава и че въпросът за снимки изобщо не се задава.
    """
    staged = tmp_path / "staged.jpg"
    staged.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f"/attach {staged}", "/wizard Нещо се случи"])

    repl.run()

    post_dir = pipeline.calls[0]["post_dir"]
    assert (post_dir / "photos" / "staged.jpg").exists()
    assert repl.state.attachments == []  # не изтича към следващ пост


def test_wizard_recognizes_at_token_in_the_inline_argument(tmp_path):
    """
    Бъг, поправен 2026-08-12: `@път` в отговор на /wizard влизаше буквално в брифа
    вместо да се разпознае като снимка — различно поведение от свободния ред, в
    зависимост кой флоу човек ползва по навик.
    """
    photo = tmp_path / "екип.jpg"
    photo.write_bytes(b"x")
    pipeline = FakePipeline()
    repl, _ = make_repl(tmp_path, pipeline, [f'/wizard Среща с екипа @"{photo}"'])

    repl.run()

    post_dir = pipeline.calls[0]["post_dir"]
    briefing = (post_dir / "briefing.md").read_text(encoding="utf-8")
    assert (post_dir / "photos" / "екип.jpg").exists()
    assert "@" not in briefing


def test_successful_post_is_copied_to_clipboard(tmp_path):
    copied: list[str] = []
    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.clipboard_fn = lambda text: (copied.append(text) or True)

    repl.run()

    assert copied == ["Готов пост."]
    assert any("копирано в клипборда" in line for line in output)


def test_clipboard_failure_does_not_break_the_session_or_claim_success(tmp_path):
    repl, output = make_repl(tmp_path, FakePipeline(), ["Бриф"])
    repl.clipboard_fn = lambda text: False

    repl.run()  # не бива да хвърли изключение

    assert not any("копирано в клипборда" in line for line in output)


def test_interrupted_run_does_not_attempt_to_copy_an_empty_clipboard(tmp_path):
    calls: list[str] = []
    repl, _ = make_repl(tmp_path, FakePipeline(raises=BriefingMissingError("x")), ["Бриф"])
    repl.clipboard_fn = lambda text: (calls.append(text) or True)

    repl.run()

    assert calls == []


def test_env_setup_is_skipped_when_env_already_exists(tmp_path):
    """Не бива да пита нищо, ако .env вече е там — иначе всяко старо пускане би питало отново."""
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=1\n", encoding="utf-8")
    example_path = tmp_path / ".env.example"
    example_path.write_text("OPENWEBUI_API_KEY=\n", encoding="utf-8")
    asked: list[str] = []

    def input_fn(prompt: str) -> str:
        asked.append(prompt)
        raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=FakePipeline(),
        env_path=env_path,
        env_example_path=example_path,
    )

    repl.run()

    # Единственото извикване е нормалният промпт на главния цикъл ("› ") — ако env
    # setup-ът се беше задействал, преди него щеше да има допълнителен въпрос.
    assert asked == ["› "]
    assert env_path.read_text(encoding="utf-8") == "EXISTING=1\n"


def test_env_setup_writes_env_from_example_with_the_provided_key(tmp_path):
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    example_path.write_text("OPENWEBUI_API_KEY=\nMODEL_WRITER=some-model\n", encoding="utf-8")
    output: list[str] = []
    inputs = iter(["да", "tайна-стойност", ""])  # да създаде, ключ, после EOF в основния цикъл

    def input_fn(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=output.append,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=FakePipeline(),
        env_path=env_path,
        env_example_path=example_path,
    )

    repl.run()

    written = env_path.read_text(encoding="utf-8")
    assert "OPENWEBUI_API_KEY=tайна-стойност" in written
    assert "MODEL_WRITER=some-model" in written
    assert any("Записан" in line for line in output)


def test_env_setup_can_be_declined(tmp_path):
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    example_path.write_text("OPENWEBUI_API_KEY=\n", encoding="utf-8")
    inputs = iter(["не"])

    def input_fn(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=FakeClient,
        pipeline_fn=FakePipeline(),
        env_path=env_path,
        env_example_path=example_path,
    )

    repl.run()

    assert not env_path.exists()


def test_split_path_args_supports_quoted_paths_with_spaces():
    from src.repl import split_path_args

    assert split_path_args('"C:\снимки папка\a.jpg" b.jpg') == ["C:\снимки папка\a.jpg", "b.jpg"]


# --------------------------------------------------------------------------- тънък бриф (План 24)


def test_thin_brief_precheck_asks_nothing_when_planner_does_not_flag_it_as_thin(tmp_path):
    """По подразбиране (plan_notes без "тънък") precheck-ът минава транзитивно, без въпрос."""
    pipeline = FakePipeline()
    repl, output = make_repl(tmp_path, pipeline, ["Обичаен бриф с достатъчно детайли."], plan_notes="")

    repl.run()

    assert pipeline.calls[0]["force"] is False
    assert not any("тънък бриф" in line for line in output)


def test_thin_brief_precheck_asks_a_question_when_planner_flags_it_as_thin(tmp_path):
    """Ако Планерът пише "тънък" в notes, REPL-ът пита ЕДНО уточняващо въпросче преди да продължи."""
    pipeline = FakePipeline()
    asked: list[str] = []
    inputs = iter(["Кратък бриф.", ""])

    def input_fn(prompt: str) -> str:
        asked.append(prompt)
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=lambda config: FakeClient(config, plan_notes="Брифът е тънък — липсват дата и конкретни детайли."),
        pipeline_fn=pipeline,
        today=date(2026, 8, 11),
    )

    repl.run()

    assert any("тънък бриф" in prompt for prompt in asked)
    # Enter (празен отговор) -> брифът остава непроменен, run_pipeline не се форсира.
    assert pipeline.calls[0]["force"] is False
    briefing = (pipeline.calls[0]["post_dir"] / "briefing.md").read_text(encoding="utf-8")
    assert briefing.strip() == "Кратък бриф."


def test_thin_brief_precheck_asks_nothing_when_planner_says_thin_but_adequate(tmp_path):
    """
    Регресионен тест за реален фалшив positive, открит при адверсариално живо тестване
    (2026-08-31, виж research-plan-25): Планерът винаги пише "тънък" за цяла категория
    брифове, включително такива с богата фактология, квалифицирайки ги с "но достатъчен/
    съдържателен" (виж planner.py's системен промпт). Стар precheck питаше дори тогава.
    """
    pipeline = FakePipeline()
    asked: list[str] = []
    inputs = iter(["Богат бриф с много факти и числа."])

    def input_fn(prompt: str) -> str:
        asked.append(prompt)
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=lambda config: FakeClient(
            config, plan_notes="Брифът е тънък, но съдържателен – съдържа 4 конкретни елемента."
        ),
        pipeline_fn=pipeline,
        today=date(2026, 8, 11),
    )

    repl.run()

    assert not any("тънък бриф" in prompt for prompt in asked)
    assert pipeline.calls[0]["force"] is False


def test_thin_brief_precheck_enriches_briefing_and_forces_rerun_when_answered(tmp_path):
    """Отговор, различен от Enter, се добавя към брифа и force=True кара конвейера да преизчисли всичко."""
    pipeline = FakePipeline()
    repl, output = make_repl(
        tmp_path,
        pipeline,
        ["Кратък бриф.", "Участваха 25 души на 14 март."],
        plan_notes="Брифът е тънък — липсват дата и конкретни детайли.",
    )

    repl.run()

    assert pipeline.calls[0]["force"] is True
    post_dir = pipeline.calls[0]["post_dir"]
    briefing = post_dir.joinpath("briefing.md").read_text(encoding="utf-8")
    assert "Кратък бриф." in briefing
    assert "Участваха 25 души на 14 март." in briefing
    assert not (post_dir / "plan.json").exists()  # старият план (върху тънката версия) е изтрит


def test_thin_brief_precheck_does_not_run_for_again_or_revise(tmp_path):
    """/again и /revise минават направо през _generate — precheck-ът важи само за нов бриф."""
    pipeline = FakePipeline()
    asked: list[str] = []
    inputs = iter(["Кратък бриф.", "", "/again"])

    def input_fn(prompt: str) -> str:
        asked.append(prompt)
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    repl = Repl(
        posts_dir=tmp_path / "posts",
        out=lambda line: None,
        input_fn=input_fn,
        ink=Ink(enabled=False),
        config_loader=make_config,
        knowledge_loader=make_brand_knowledge,
        client_factory=lambda config: FakeClient(config, plan_notes="Брифът е тънък — липсват дата и конкретни детайли."),
        pipeline_fn=pipeline,
        today=date(2026, 8, 11),
    )

    repl.run()

    # Едно "тънък бриф" питане общо (само за първоначалния бриф, не и за /again).
    assert sum(1 for prompt in asked if "тънък бриф" in prompt) == 1
    assert len(pipeline.calls) == 2
    assert pipeline.calls[1]["force"] is True  # /again винаги force, независимо от precheck-а
