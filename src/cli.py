"""
Входна точка: `python -m src.cli [команда] ...`

БЕЗ команда се отваря интерактивна сесия (виж src/repl.py) — човек пише бриф на
промпта, агентът отпечатва готовия пост, /revise го дошлайфа. Това е нормалният
начин за работа; командите по-долу остават за скриптове и еднократни проверки.

Команди:
  chat                             — същото като без команда: интерактивна сесия
  generate <post_dir> [--force] [--brief "текст"]   — минава през седемте стъпки на конвейера
                                                        (--brief пише директно briefing.md,
                                                        създава папката, ако не съществува —
                                                        без нужда от ръчно отваряне на файл)
  new <име>                        — създава posts/<име> от шаблона posts/_example
  check                            — пробва връзката с Open WebUI за всеки конфигуриран модел
  check-knowledge                  — показва кои knowledge/ файлове липсват/са празни
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
from pathlib import Path

from src.brand.loader import audit_knowledge
from src.config import ConfigError, load_config
from src.errors import AgentError
from src.llm.client import LLMClient
from src.pipeline.runner import run_pipeline
from src.terminal import force_utf8_io

POSTS_DIR = Path("posts")
EXAMPLE_DIR = POSTS_DIR / "_example"


def cmd_generate(args: argparse.Namespace) -> int:
    """Пуска конвейера за една папка. С --brief пише briefing.md вместо да го изисква готов."""
    post_dir = Path(args.post_dir)
    brief_text = getattr(args, "brief", None)

    force = args.force

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

    try:
        result = run_pipeline(post_dir, client, force=force)
    except AgentError as exc:
        # Единствените изключения тук са за вход/инфраструктура — виж докстринга на runner.py.
        print(f"\n✗ Спряно: {exc.human_message}")
        print(f"Пълен лог: {post_dir / 'run.log'}")
        return 1

    # Отвъд тази точка конвейерът винаги е успял — само печатаме резюме и открити въпроси.
    merged = ", ".join(result.merged_from) or "?"
    print(f"\n✓ Официалното предложение е в: {result.post_path} (от {merged})")

    if result.applied_fixes:
        print(f"\n✎ {len(result.applied_fixes)} автоматична(и) поправка(и) по правилата от knowledge/voice/:")
        for fix in result.applied_fixes:
            print(f"  - {fix}")

    if result.open_notes:
        print(f"\n⚠ {len(result.open_notes)} открит(и) въпрос(а) в предложението — прегледайте преди публикуване:")
        for note in result.open_notes:
            print(f"  - {note}")

    if result.warnings:
        print(f"\n⚠ {len(result.warnings)} предупреждение(я) за снимки:")
        for warning in result.warnings:
            print(f"  - {warning}")

    print(f"\nПълен лог: {post_dir / 'run.log'}")
    return 0


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
        return cmd_chat(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
