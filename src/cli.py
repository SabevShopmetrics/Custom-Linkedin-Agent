"""
Входна точка: `python -m src.cli <команда> ...`

Команди:
  generate <post_dir> [--force] [--brief "текст"]   — минава през шестте стъпки на конвейера
                                                        (--brief пише директно briefing.md,
                                                        създава папката, ако не съществува —
                                                        без нужда от ръчно отваряне на файл)
  new <име>                        — създава posts/<име> от шаблона posts/_example
  check                            — пробва връзката с Open WebUI за всеки конфигуриран модел
  check-knowledge                  — показва кои knowledge/ файлове липсват/са празни
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

POSTS_DIR = Path("posts")
EXAMPLE_DIR = POSTS_DIR / "_example"


def _force_utf8_console() -> None:
    """
    Windows конзолата по подразбиране е cp1252, което гърми на кирилица.
    Пренасочваме stdout/stderr към UTF-8, ако терминалът го поддрев.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except Exception:  # pragma: no cover — най-добро усилие, никога не бива да чупи CLI-то
                pass


def cmd_generate(args: argparse.Namespace) -> int:
    post_dir = Path(args.post_dir)
    brief_text = getattr(args, "brief", None)

    force = args.force

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
        print(f"\n✗ Спряно: {exc.human_message}")
        print(f"Пълен лог: {post_dir / 'run.log'}")
        return 1

    if result.status == "ready":
        print(f"\n✓ Готово. Постът е в: {result.post_path}")
    else:
        print(f"\n⚠ Статус '{result.status}' — прегледайте ръчно: {result.output_dir}")

    if result.warnings:
        print(f"\n⚠ {len(result.warnings)} предупреждение(я) (GDPR/снимки) — не спират поста, но прегледайте преди публикуване:")
        for warning in result.warnings:
            print(f"  - {warning}")

    print(f"\nПълен лог: {post_dir / 'run.log'}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
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
    audit = audit_knowledge()

    print("knowledge/ — файлове:\n")
    for entry in audit["files"]:
        if not entry.exists:
            status = "ЛИПСВА"
        elif entry.empty:
            status = "ПРАЗЕН"
        else:
            status = "OK"
        print(f"  [{status:7}] {entry.path}")

    print(f"\nidentity/facts.yaml наличен: {'да' if audit['facts_available'] else 'не'}")
    if audit["facts_null_fields"]:
        print("Непотвърдени (null) полета във facts.yaml:")
        for field_path in audit["facts_null_fields"]:
            print(f"  - {field_path}")
    elif audit["facts_available"]:
        print("Всички полета във facts.yaml са попълнени.")

    print(f"\nmembers/members.yaml: {audit['members_count']} организации")
    if audit["members_with_notes"]:
        print("Записи с бележка (напр. разминаване сайт/LinkedIn):")
        for member in audit["members_with_notes"]:
            print(f"  - {member.get('name', '?')}: {member.get('notes')}")

    print(f"\nexamples/good/: {audit['good_examples_count']} примера")
    print(f"examples/bad/: {audit['bad_examples_count']} примера")

    missing_or_empty = [e.path for e in audit["files"] if not e.usable]
    if missing_or_empty or not audit["facts_available"] or audit["members_count"] == 0:
        print(
            "\n⚠ knowledge/ е частично попълнено — веригата ще работи, но с ограничено "
            "качество (Критикът ще флагва повече твърдения като недоказани)."
        )
        return 0

    print("\n✓ knowledge/ е напълно попълнено.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="LinkedIn агент — ИКТ Клъстер Варна")
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
