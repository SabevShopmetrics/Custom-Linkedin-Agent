"""
Входна точка: `python -m src.cli [команда] ...` — или направо `linkedin-agent` в PowerShell
(script entry point от pyproject.toml, `pip install -e .` веднъж го регистрира на PATH).

БЕЗ команда (или `linkedin-agent` без аргументи) се отваря readline REPL-ът (виж src/repl.py)
— обратно към подразбирането отпреди 2026-08-17. Причина за връщането (потребителска
инструкция 2026-08-25): full-screen TUI-то (src/tui.py) грабва алтернативния екран на
терминала, а това изключва native scrollback на терминала самия — за разлика от Claude Code
CLI (и от REPL-а тук), които пишат нормален, скролиращ изход и не пипат alt-screen въобще.
Няма компромис между двете: докато full-screen приложение държи alt-screen, никой terminal
emulator не предлага собствен scrollback върху него — фундаментално ограничение на терминалите,
не на Textual. TUI-то остава достъпно изрично през `tui`, за който предпочита „премиум"
изгледа и приема тази цена.

Команди:
  chat                             — readline REPL (ред по ред, /команди) — същото и БЕЗ команда
  tui                              — full-screen интерфейс (Textual); БЕЗ native terminal scrollback
  web [--host H] [--port P] [--open] — уеб интерфейс в браузъра: бриф, снимки и ZIP архиви,
                                     преглед „както в LinkedIn" (виж src/web/server.py)
  generate <post_dir> [--force] [--brief "текст"] [--mode кадър|фокус|панорама]
                                     — минава през седемте стъпки на конвейера (--brief пише
                                       директно briefing.md, създава папката, ако не
                                       съществува; --mode избира дължина, виж
                                       src/pipeline/modes.py, по подразбиране "фокус")
  new <име>                        — създава posts/<име> от шаблона posts/_example
  preview <post_dir>               — output/preview.html (текст + избрани снимки), за визуален
                                     преглед в браузър на вече завършен пост
  check                            — пробва връзката с Open WebUI за всеки конфигуриран модел
  check-knowledge                  — показва кои knowledge/ файлове липсват/са празни
  spike [--brief ФАЙЛ] [--repeat N] — мери на живо опционалните ускорявания (research-plan-36)
  bench [--only ПРЕФИКС] [--save ФАЙЛ] [--compare ФАЙЛ]
                                   — мери качеството на вече направените пускания в posts/
                                     (без нито едно извикване към модел) и го сравнява с
                                     референтната лента от дословните постове в корпуса.
                                     Виж docs/research-plans/research-plan-11-text-quality.md
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

from src.brand.loader import audit_knowledge
from src.config import ConfigError, load_config
from src.errors import AgentError
from src.humanize import humanize_notes, status_label
from src.llm.client import LLMClient
from src.pipeline import modes
from src.pipeline.preview import render_preview
from src.pipeline.runner import FinalCheckUpdate, run_pipeline
from src.pipeline.util import file_uri
from src.terminal import force_utf8_io

POSTS_DIR = Path("posts")
EXAMPLE_DIR = POSTS_DIR / "_example"


def cmd_generate(args: argparse.Namespace) -> int:
    """Пуска конвейера за една папка. С --brief пише briefing.md вместо да го изисква готов."""
    post_dir = Path(args.post_dir)
    brief_text = getattr(args, "brief", None)

    force = args.force

    # getattr с подразбиране: тестове/по-стари извиквания конструират Namespace без .mode —
    # трябва да пада на подразбиращия се режим ("фокус"), не да гърми с AttributeError.
    mode_arg = getattr(args, "mode", None) or modes.DEFAULT_MODE
    mode_key = modes.resolve_mode(mode_arg)
    if mode_key is None:
        print(f"Грешка: непознат --mode '{mode_arg}'. Налични: {modes.mode_list_text()}")
        return 1

    # --brief е кратък път: пише briefing.md директно от команден ред, вместо `new` +
    # ръчно редактиране на файл.
    if brief_text:
        # --brief замества нуждата от ръчно създаване/редактиране на briefing.md — папката
        # се създава при нужда, briefing.md се презаписва изрично с подадения текст.
        # Форсираме пълно преизчисление — иначе стари plan.json/post_i.md от предишно
        # пускане в същата папка биха се преизползвали и биха игнорирали новия бриф.
        (post_dir / "photos").mkdir(parents=True, exist_ok=True)
        (post_dir / "briefing.md").write_text(brief_text.strip() + "\n", encoding="utf-8")
        print(f"Записан briefing.md в '{post_dir}' от --brief.")
        force = True
    elif not post_dir.exists():
        print(f"Грешка: папката '{post_dir}' не съществува. Използвайте 'new' за да я създадете, или подайте --brief.")
        return 1

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Грешка в конфигурацията: {exc}")
        return 1

    client = LLMClient(config)

    # research-plan-36, А2: постът се печата ВЕДНАГА, а финалната проверка [7] се изчаква
    # след това (до същия таван от 90s — план 29/35). Преди `run_pipeline(...,
    # wait_for_final_check_seconds=90)` блокираше ВЪТРЕ в конвейера и човекът гледаше празен
    # екран до 90s за вече готов текст. План 35 (Критика 1) твърдеше обратното — грешно.
    final_check_done = threading.Event()
    final_updates: list[FinalCheckUpdate] = []

    def on_final_check(update: FinalCheckUpdate) -> None:
        final_updates.append(update)
        final_check_done.set()

    try:
        result = run_pipeline(post_dir, client, force=force, mode=mode_key, on_final_check=on_final_check)
    except AgentError as exc:
        # Единствените изключения тук са за вход/инфраструктура — виж докстринга на runner.py.
        print(f"\n✗ Спряно: {exc.human_message}")
        if exc.detail:
            # Суровата причина (HTTP статус, грешка при декодиране на снимка и т.н.) — без нея
            # човекът вижда само общото съобщение и няма как да прецени дали проблемът е
            # наистина недостъпен модел, или нещо друго (напр. развален входен файл).
            print(f"  Причина: {exc.detail}")
        print(f"Пълен лог: {post_dir / 'run.log'}")
        return 1

    # Отвъд тази точка конвейерът винаги е успял — печатаме текста, после резюме и бележки.
    post_text = (result.post_path.read_text(encoding="utf-8") if result.post_path.exists() else "").strip()
    print("\n" + "─" * 60)
    print(post_text)
    print("─" * 60)
    merged = ", ".join(result.merged_from) or "?"
    print(f"\n✓ {status_label(result.status)} — {result.post_path} (от {merged})")
    print(f"  файл:    {file_uri(result.post_path)}")

    # Автоматичен преглед след всяко успешно пускане (потребителска заявка 2026-08-18) —
    # преди изискваше отделно `preview` извикване. Best-effort: липсваща/развалена снимка тук
    # не бива да съсипва вече успешния резултат от пълния конвейер.
    try:
        preview_path = render_preview(post_dir)
        print(f"  преглед: {file_uri(preview_path)}")
        # Автоматично отваряне в браузъра (потребителска обратна връзка 2026-09-03: "не се
        # отваря директно в браузъра", преглед само на разположение като линк за ръчно
        # копиране). Best-effort — среда без графичен браузър (сървър, cron) не бива да
        # чупи вече успешния резултат.
        try:
            webbrowser.open(file_uri(preview_path))
        except Exception:
            pass
    except (FileNotFoundError, OSError) as exc:
        print(f"  ⚠ Прегледът не се генерира: {exc}")

    if result.resolution_summary:
        print(f"\n{result.resolution_summary}")

    if result.applied_fixes:
        print(f"\n✎ {len(result.applied_fixes)} автоматична(и) поправка(и) по правилата от knowledge/voice/:")
        for fix in result.applied_fixes:
            print(f"  - {fix}")

    notes = humanize_notes(result.open_notes + result.warnings)
    if notes:
        print(f"\n⚠ Проверете преди публикуване ({len(notes)}):")
        for note in notes:
            print(f"  - {note}")

    meta_final = _final_check_status(result.meta_path)
    if meta_final == "pending":
        print("\n… Проверявам окончателния текст още веднъж (до 90 сек.) — текстът по-горе вече е готов.")
        final_check_done.wait(timeout=90.0)
        if final_updates:
            update = final_updates[0]
            late = humanize_notes(update.notes)
            if update.autofixed:
                # Стъпка [8] (research-plan-38): текстът по-горе вече е сменен — показваме новия.
                print(f"\n✓ Агентът махна сам {len(update.autofixed)} недоказани неща:")
                for claim in update.autofixed:
                    print(f"  - {claim}")
                fixed_text = (result.post_path.read_text(encoding="utf-8") if result.post_path.exists() else "").strip()
                print("\nПоправеният пост:\n")
                print(fixed_text)
            if update.status == "failed":
                print("  Окончателната проверка не успя — прочетете поста внимателно.")
            elif late:
                print(f"⚠ Окончателната проверка намери още {len(late)}:")
                for note in late:
                    print(f"  - {note}")
            else:
                print("✓ Окончателната проверка завърши — няма нови бележки.")
            try:
                render_preview(post_dir)  # закъснелите бележки влизат и в прегледа
            except (FileNotFoundError, OSError):
                pass
        else:
            print("  Проверката не завърши навреме — продължава във фонов режим само докато процесът е жив.")

    print(f"\nПълен лог: {post_dir / 'run.log'}")
    return 0


def _final_check_status(meta_path: Path) -> str | None:
    import json

    try:
        return (json.loads(meta_path.read_text(encoding="utf-8")).get("final_check") or {}).get("status")
    except (OSError, ValueError):
        return None


def cmd_new(args: argparse.Namespace) -> int:
    """Копира шаблона от posts/_example/ в posts/<name> — стартова точка за ръчно писане на бриф."""
    target = POSTS_DIR / args.name
    if target.exists():
        print(f"Грешка: '{target}' вече съществува.")
        return 1
    if not EXAMPLE_DIR.exists():
        print(f"Грешка: липсва шаблонът '{EXAMPLE_DIR}'.")
        return 1

    shutil.copytree(EXAMPLE_DIR, target)
    print(f"Създадено: {target}")
    print(f"Редактирайте {target / 'briefing.md'} и сложете снимки в {target / 'photos'}.")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """Пробва всеки конфигуриран модел с минимален промпт — за диагностика преди реално пускане."""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Грешка в конфигурацията: {exc}")
        return 1

    client = LLMClient(config)
    results = client.check_connection()

    all_ok = True
    print(f"Open WebUI: {config.openwebui_base_url}\n")
    for label, info in results.items():
        status = "OK" if info["ok"] else "FAIL"
        line = f"  [{status:4}] {label:14} {info['model']:40} {info['duration_seconds']}s"
        print(line)
        if not info["ok"]:
            all_ok = False
            print(f"           └─ {info['error']}")

    return 0 if all_ok else 1


def cmd_check_knowledge(_args: argparse.Namespace) -> int:
    """Отпечатва пълния одит на knowledge/ — кои файлове/полета липсват или са празни."""
    audit = audit_knowledge()

    # Секция 1: статус на всеки markdown файл (OK / ПРАЗЕН / ЛИПСВА).
    print("knowledge/ — файлове:\n")
    for entry in audit["files"]:
        if not entry.exists:
            status = "ЛИПСВА"
        elif entry.empty:
            status = "ПРАЗЕН"
        else:
            status = "OK"
        print(f"  [{status:7}] {entry.path}")

    # Секция 2: facts.yaml — кои полета съществуват, но са null (непотвърдени).
    print(f"\nidentity/facts.yaml наличен: {'да' if audit['facts_available'] else 'не'}")
    if audit["facts_null_fields"]:
        print("Непотвърдени (null) полета във facts.yaml:")
        for field_path in audit["facts_null_fields"]:
            print(f"  - {field_path}")
    elif audit["facts_available"]:
        print("Всички полета във facts.yaml са попълнени.")

    # Секция 3: членове — колко общо, колко с потвърдено съгласие за споменаване по име.
    print(
        f"\nmembers/members.yaml: {audit['members_count']} организации, "
        f"{audit['members_mentionable_count']} с потвърдено съгласие за споменаване "
        f"(mention_allowed: true)"
    )
    if audit["members_count"] and not audit["members_mentionable_count"]:
        print(
            "  ⚠ Нито един член няма потвърдено съгласие — Писателят ползва само имената от "
            "брифа и не добавя членове по своя преценка."
        )
    if audit["members_with_notes"]:
        print("Записи с бележка (напр. разминаване сайт/LinkedIn):")
        for member in audit["members_with_notes"]:
            print(f"  - {member.get('name', '?')}: {member.get('notes')}")

    # Секция 4: еталонни примери — дословни (влизат в промпта) срещу обобщения (не влизат).
    print(f"\nexamples/good/: {audit['good_examples_count']} дословни поста (еталон за подражание)")
    if audit["summary_examples_count"]:
        print(
            f"  + {audit['summary_examples_count']} обобщения — НЕ влизат в промпта "
            f"(не са дословен текст; виж план 11, Фаза 0)"
        )
    print(f"examples/bad/: {audit['bad_examples_count']} примера")

    # research-plan-36, Г4: размерът на общия промпт и „бележки за разработчици" в него.
    print(f"\nОбщ контекст в промпта: {audit['stable_context_chars']} знака (след почистване 2026-10-09: ~12.5k)")
    if audit["dated_notes_in_prompt"]:
        print("  ⚠ Датирани бележки за разработчици стигат до модела — сложете ги в <!-- коментар -->:")
        for line in audit["dated_notes_in_prompt"]:
            print(f"    - {line}")

    # Финалната присъда: и без пълен корпус конвейерът работи (връща 0 винаги, само
    # предупреждава) — return code 1 тук би спрял CI/скриптове напразно.
    missing_or_empty = [e.path for e in audit["files"] if not e.usable]
    if missing_or_empty or not audit["facts_available"] or audit["members_count"] == 0:
        print(
            "\n⚠ knowledge/ е частично попълнено — веригата ще работи, но с ограничено "
            "качество (Критикът ще флагва повече твърдения като недоказани)."
        )
        return 0

    print("\n✓ knowledge/ е напълно попълнено.")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """
    Базовата линия от план 11, Фаза 1. Нарочно НЕ вика модел: чете вече съществуващите
    пускания в posts/ и мери какво има в тях, за да е безплатно и повтаряемо.
    """
    from src.quality import bench

    report = bench.collect(POSTS_DIR, only=args.only)
    previous = bench.load(Path(args.compare)) if args.compare else None
    if args.compare and previous is None:
        print(f"⚠ {args.compare} не е намерен или не е валиден JSON — сравнението се пропуска.\n")

    print(bench.render(report, previous))

    if args.save:
        saved = bench.save(report, Path(args.save))
        print(f"\nОтчетът е записан в {saved}")

    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Генерира output/preview.html (текст + избрани снимки, вградени в самостоятелен файл)."""
    from src.pipeline.preview import render_preview

    post_dir = Path(args.post_dir)
    try:
        preview_path = render_preview(post_dir)
    except FileNotFoundError as exc:
        print(f"Грешка: {exc}")
        return 1

    print(f"Прегледът е записан в: {preview_path}")
    return 0


def cmd_spike(args: argparse.Namespace) -> int:
    """Мери на живо опционалните ускорявания (research-plan-36, Б3/Б4) — виж src/spike.py."""
    from src.spike import run_spike

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Грешка в конфигурацията: {exc}")
        return 1
    return run_spike(config, brief_path=Path(args.brief), repeat=args.repeat)


def cmd_tui(_args: argparse.Namespace) -> int:
    # Импортът е вътре във функцията по същата причина като cmd_chat — textual/rich не се
    # плащат при `generate`/`check` в скрипт или cron.
    from src.tui import main as tui_main

    return tui_main()


def cmd_web(args: argparse.Namespace) -> int:
    """Уеб интерфейс в браузъра — виж src/web/server.py. Настройки: WEB_HOST/WEB_PORT/WEB_PASSWORD в .env."""
    import os

    from dotenv import load_dotenv

    from src.web.server import DEFAULT_PORT, serve

    load_dotenv(override=False)
    host = args.host or os.environ.get("WEB_HOST", "").strip() or "127.0.0.1"
    port_raw = str(args.port or os.environ.get("WEB_PORT", "").strip() or DEFAULT_PORT)
    if not port_raw.isdigit():
        print(f"Грешка: WEB_PORT трябва да е число, получено: '{port_raw}'")
        return 1
    password = os.environ.get("WEB_PASSWORD", "").strip() or None
    max_jobs_raw = os.environ.get("WEB_MAX_JOBS", "").strip()
    max_jobs = int(max_jobs_raw) if max_jobs_raw.isdigit() else 3
    # Браузърът се отваря само при изрично --open (start_web.bat) — никога от тестове/скриптове.
    opener = (lambda url: webbrowser.open(url)) if args.open else None
    return serve(
        host=host,
        port=int(port_raw),
        password=password,
        posts_dir=POSTS_DIR,
        open_browser=opener,
        max_jobs=max_jobs,
        tls_cert=os.environ.get("WEB_TLS_CERT", "").strip() or None,
        tls_key=os.environ.get("WEB_TLS_KEY", "").strip() or None,
    )


def cmd_chat(_args: argparse.Namespace) -> int:
    # Импортът е вътре във функцията, за да не се плаща цената му при `generate`/`check`
    # в скрипт или cron — repl.py тегли конфигурация, knowledge и терминални неща.
    from src.repl import Repl

    return Repl().run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="LinkedIn агент — ИКТ Клъстер Варна (без команда: интерактивна сесия)",
    )
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="интерактивна сесия в терминала (по подразбиране)")
    chat_parser.set_defaults(func=cmd_chat)

    tui_parser = subparsers.add_parser("tui", help="full-screen интерфейс (Textual)")
    tui_parser.set_defaults(func=cmd_tui)

    web_parser = subparsers.add_parser("web", help="уеб интерфейс в браузъра (бриф, снимки/ZIP, преглед)")
    web_parser.add_argument("--host", default=None, help="адрес за слушане (подразбиране WEB_HOST или 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=None, help="порт (подразбиране WEB_PORT или 8765)")
    web_parser.add_argument("--open", action="store_true", help="отваря интерфейса в браузъра след старт")
    web_parser.set_defaults(func=cmd_web)

    generate_parser = subparsers.add_parser("generate", help="генерира пост от бриф + снимки")
    generate_parser.add_argument("post_dir", help="папка на поста, напр. posts/2026-07-praktiki")
    generate_parser.add_argument("--force", action="store_true", help="преизчислява всички стъпки наново")
    generate_parser.add_argument(
        "--brief",
        default=None,
        help=(
            "текст на брифа директно от команден ред — записва се в briefing.md, "
            "папката се създава ако липсва (без нужда от 'new' + ръчно редактиране)"
        ),
    )
    generate_parser.add_argument(
        "--mode",
        default=modes.DEFAULT_MODE,
        help=(
            "дължина на поста: кадър/kadar (кратко), фокус/fokus (по подразбиране), "
            "панорама/panorama (наистина дълго) — виж src/pipeline/modes.py"
        ),
    )
    generate_parser.set_defaults(func=cmd_generate)

    new_parser = subparsers.add_parser("new", help="създава нова папка за пост от шаблона")
    new_parser.add_argument("name", help="име на новата папка, напр. 2026-07-praktiki")
    new_parser.set_defaults(func=cmd_new)

    check_parser = subparsers.add_parser("check", help="проверява връзката с Open WebUI и моделите")
    check_parser.set_defaults(func=cmd_check)

    check_knowledge_parser = subparsers.add_parser(
        "check-knowledge", help="показва кои knowledge/ файлове липсват/са празни"
    )
    check_knowledge_parser.set_defaults(func=cmd_check_knowledge)

    preview_parser = subparsers.add_parser(
        "preview", help="генерира output/preview.html (текст + избрани снимки) за вече завършен пост"
    )
    preview_parser.add_argument("post_dir", help="папка на вече завършен пост, напр. posts/2026-07-praktiki")
    preview_parser.set_defaults(func=cmd_preview)

    spike_parser = subparsers.add_parser(
        "spike", help="мери на живо дали изключване на мисленето / гарантиран JSON ускоряват агента"
    )
    spike_parser.add_argument("--brief", default="bench/briefs/02-bogat-brief-sabitie.md", help="бриф за пробата")
    spike_parser.add_argument("--repeat", type=int, default=2, help="колко пъти всеки вариант (медиана)")
    spike_parser.set_defaults(func=cmd_spike)

    bench_parser = subparsers.add_parser(
        "bench", help="мери качеството на вече направените пускания (без извикване към модел)"
    )
    bench_parser.add_argument(
        "--only",
        default=None,
        help="анализира само папки с този префикс, напр. --only bench- (контролиран набор брифове)",
    )
    bench_parser.add_argument(
        "--save", default=None, help="записва отчета като JSON, напр. --save bench/baseline.json"
    )
    bench_parser.add_argument(
        "--compare", default=None, help="сравнява с по-стар записан отчет, напр. --compare bench/baseline.json"
    )
    bench_parser.set_defaults(func=cmd_bench)

    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # Без подкоманда — readline REPL (потребителска инструкция 2026-08-25: връща
        # подразбирането отпреди 2026-08-17 заради native terminal scrollback — виж
        # докстринга на модула). Full-screen TUI остава достъпно изрично през `tui`.
        return cmd_chat(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
