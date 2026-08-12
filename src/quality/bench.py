"""
Базова линия за качеството на текста — план 11, Фаза 1.

Целта е едно нещо, което дотогава липсваше: **число, с което да се сравнява.** Без него
всяка промяна в промпт, модел или верига е гадаене, а влошаването е невидимо: веригата
произведе предложение с по-малко факти от собствената си първа чернова в 7 от 20 пускания
и с по-малко думи в 13 от 20, без нищо да го отбележи.

Първото пускане на самия инструмент веднага прекалибрира диагнозата от план 11: плътността
(факти на 100 думи) почти не се мени между Post I и предложението (медиана 10.1 → 10.2),
докато абсолютният брой факти пада (10 → 9). Тоест веригата не разрежда текста — тя
изхвърля цели факти заедно с изреченията, които ги носят. Затова тук се мерят и двете.

Инструментът НЕ вика модел. Чете вече съществуващите папки в `posts/` и мери какво има в
тях. Затова е безплатен, повтаряем и може да се пусне върху историята — включително преди
и след промяна, стига пусканията да са направени със същия бриф.

Кое се мери и защо точно то — виж src/quality/metrics.py. Важното тук е СРАВНЕНИЕТО между
трите текста в едно пускане (Post I → Post II → предложение): то показва коя стъпка добавя
конкретност и коя маха. Агрегат по 20 папки скрива точно това.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.brand.loader import load_brand_knowledge
from src.llm.schema import CritiqueSchema, FactSource
from src.pipeline import consolidator, critic
from src.quality.metrics import ReferenceBand, TextMetrics, measure, reference_band

# Находка на Критика, която говори за механично правило. Такива НЕ бива да съществуват:
# броенето на емоджи, хаштагове, думи и адреси е в кода от план 10, а промптът на Критика
# изрично му забранява да ги коментира. Реален случай 2026-08-11: Критикът твърди „само 1
# емоджи", когато са 4 — преброил уникални символи, не срещания. Затова тази категория се
# мери отделно и целта ѝ е нула.
_MECHANICAL_MARKERS = (
    "емоджи",
    "emoji",
    "хаштаг",
    "hashtag",
    "брой думи",
    "дължина",
    "думи, под",
    "думи, над",
    "минимален брой",
    "максимум",
)


@dataclass
class PostAnalysis:
    """Едно пускане: как се променя текстът през стъпките и какво е намерил Критикът."""

    name: str
    status: str
    merged_from: list[str]
    stages: dict[str, TextMetrics]  # "post_i" / "post_ii" / "official"
    findings_mechanical: int
    findings_tone: int
    findings_factual: int
    findings_rules: int
    synthesis_happened: bool | None  # None, когато втори пост няма

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "merged_from": self.merged_from,
            "stages": {stage: m.to_dict() for stage, m in self.stages.items()},
            "findings": {
                "mechanical": self.findings_mechanical,
                "tone": self.findings_tone,
                "factual": self.findings_factual,
                "rules": self.findings_rules,
            },
            "synthesis_happened": self.synthesis_happened,
        }


@dataclass
class BenchReport:
    """Резултатът от collect() — референтната лента плюс анализ на всяко пускане в posts/."""

    reference: ReferenceBand | None
    posts: list[PostAnalysis] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_band": self.reference.to_dict() if self.reference else None,
            "aggregate": self.aggregate(),
            "posts": [p.to_dict() for p in self.posts],
        }

    def aggregate(self) -> dict[str, Any]:
        """
        Медиани по стъпка + двете числа, които всъщност интересуват: колко пъти
        предложението е по-бедно от Post I, и колко механични находки е произвел Критикът.
        """
        if not self.posts:
            return {}

        def medians(stage: str) -> dict[str, float] | None:
            values = [p.stages[stage] for p in self.posts if stage in p.stages]
            if not values:
                return None
            return {
                "words": round(statistics.median(m.words for m in values), 1),
                "facts": round(statistics.median(m.facts for m in values), 1),
                "density": round(statistics.median(m.density for m in values), 1),
                "empty_sentence_share": round(
                    statistics.median(m.empty_sentence_share for m in values), 1
                ),
                "count": len(values),
            }

        degraded = [
            p
            for p in self.posts
            if "post_i" in p.stages
            and "official" in p.stages
            and p.stages["official"].facts < p.stages["post_i"].facts
        ]
        with_second = [p for p in self.posts if p.synthesis_happened is not None]

        return {
            "runs": len(self.posts),
            "post_i": medians("post_i"),
            "post_ii": medians("post_ii"),
            "official": medians("official"),
            "official_has_fewer_facts_than_post_i": len(degraded),
            "synthesis_skipped": sum(1 for p in with_second if p.synthesis_happened is False),
            "runs_with_second_post": len(with_second),
            "mechanical_findings_total": sum(p.findings_mechanical for p in self.posts),
            "opens_with_fact": sum(
                1 for p in self.posts if "official" in p.stages and p.stages["official"].opens_with_fact
            ),
        }


def _classify_findings(critique: CritiqueSchema | None) -> tuple[int, int, int, int]:
    """(механични, тонови, фактически, правила) — механичните трябва да са нула."""
    if critique is None:
        return 0, 0, 0, 0

    mechanical = 0
    tone = 0
    for issue in critique.tone_issues:
        lowered = issue.lower()
        if any(marker in lowered for marker in _MECHANICAL_MARKERS):
            mechanical += 1
        else:
            tone += 1

    factual = sum(1 for fact in critique.facts if fact.source is FactSource.UNVERIFIED)
    return mechanical, tone, factual, len(critique.rule_violations)


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def analyze_post_dir(post_dir: Path) -> PostAnalysis | None:
    """Мери едно пускане. Връща None, ако папката не съдържа поне първа чернова."""
    post_i = _read(post_dir / "post_i.md")
    if post_i is None:
        return None  # пускане, което не е стигнало дори до Post I — няма какво да се мери

    post_ii = _read(post_dir / "post_ii.md")
    official = _read(post_dir / "output" / "post.md")

    # Измерва отделно всяка налична стъпка — липсваща стъпка (напр. няма Post II, защото
    # Критикът не намери находки) просто отсъства от `stages`, не е грешка.
    stages: dict[str, TextMetrics] = {"post_i": measure(post_i)}
    if post_ii:
        stages["post_ii"] = measure(post_ii)
    if official:
        stages["official"] = measure(official)

    meta_path = post_dir / "output" / "meta.json"
    status, merged_from = "?", []
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status = meta.get("status", "?")
            merged_from = meta.get("merged_from", [])
        except json.JSONDecodeError:
            status = "невалиден meta.json"

    # Същата функция, която конвейерът ползва, за да вдигне открит въпрос — нарочно, а не
    # собствено сравнение тук: две дефиниции на „синтезът се случи ли" биха давали различен
    # отговор за един и същи пост и отчетът би противоречил на meta.json.
    synthesis_happened: bool | None = None
    if post_ii and official:
        synthesis_happened = consolidator.synthesis_bypassed(official, post_i, post_ii) is None

    mechanical, tone, factual, rules = _classify_findings(critic.load_critique(post_dir / "critique.md"))

    return PostAnalysis(
        name=post_dir.name,
        status=status,
        merged_from=merged_from,
        stages=stages,
        findings_mechanical=mechanical,
        findings_tone=tone,
        findings_factual=factual,
        findings_rules=rules,
        synthesis_happened=synthesis_happened,
    )


def collect(posts_dir: Path = Path("posts"), *, only: str | None = None) -> BenchReport:
    """
    Всички пускания в `posts/`, по азбучен ред. `only` е префикс за филтриране
    (напр. `bench-`), за да може базова линия да се вземе само от контролирания набор
    брифове, а не от случайните пускания по време на работа.
    """
    # Референтната лента идва от корпуса, не от posts/ — сравнението е "генерирано срещу
    # реално публикувано", не "ново срещу старо генерирано".
    knowledge = load_brand_knowledge()
    report = BenchReport(reference=reference_band(knowledge.good_examples))

    if not posts_dir.is_dir():
        return report

    for child in sorted(posts_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue  # "_example" е шаблон, не пускане
        if only and not child.name.startswith(only):
            continue
        analysis = analyze_post_dir(child)
        if analysis:
            report.posts.append(analysis)

    return report


def render(report: BenchReport, previous: dict[str, Any] | None = None) -> str:
    """Табличен изглед за терминала. `previous` е зареден по-стар отчет за сравнение."""
    lines: list[str] = []

    # Блок 1: референтната лента (какво е "нормално" според корпуса).
    if report.reference:
        r = report.reference
        lines.append(
            f"Референтна лента от {r.sample_size} дословни поста в knowledge/examples/good/:\n"
            f"  думи {r.words_min}–{r.words_max} (медиана {r.words_median}) · "
            f"факти медиана {r.facts_median} · плътност медиана {r.density_median}/100 думи · "
            f"празни изречения медиана {r.empty_sentence_share_median}%"
        )
    else:
        lines.append("⚠ Няма дословни примери в knowledge/examples/good/ — липсва референтна лента.")

    if not report.posts:
        lines.append("\nНяма пускания за анализ.")
        return "\n".join(lines)

    # Блок 2: ред на пускане, показващ прехода Post I → Post II → предложение.
    lines.append("")
    lines.append(
        f"{'пускане':34}{'думи I→II→изх':>17}{'факти I→II→изх':>16}{'плътност изх':>14}{'празни изх':>12}{'мех.':>6}"
    )
    lines.append("─" * 99)
    for post in report.posts:
        def val(stage: str, attr: str) -> str:
            metrics = post.stages.get(stage)
            if metrics is None:
                return "–"
            value = getattr(metrics, attr)
            return f"{value:g}"

        words = f"{val('post_i','words')}→{val('post_ii','words')}→{val('official','words')}"
        facts = f"{val('post_i','facts')}→{val('post_ii','facts')}→{val('official','facts')}"
        empty = val("official", "empty_sentence_share")
        flag = "" if post.findings_mechanical == 0 else f" ⚠{post.findings_mechanical}"
        lines.append(
            f"{post.name[:33]:34}{words:>17}{facts:>16}{val('official','density'):>14}{empty:>12}{flag:>6}"
        )

    # Блок 3: агрегирани медиани + двете ключови числа (виж aggregate()).
    agg = report.aggregate()
    lines.append("")
    lines.append("Медиани:")
    for stage, label in (("post_i", "Post I"), ("post_ii", "Post II"), ("official", "предложение")):
        stage_agg = agg.get(stage)
        if stage_agg:
            lines.append(
                f"  {label:12} думи {stage_agg['words']:>6} · факти {stage_agg['facts']:>5} · "
                f"плътност {stage_agg['density']:>5} · "
                f"празни изречения {stage_agg['empty_sentence_share']:>5}%  (n={stage_agg['count']})"
            )

    lines.append("")
    lines.append(
        f"  предложението съдържа по-малко факти от Post I: "
        f"{agg['official_has_fewer_facts_than_post_i']} от {agg['runs']}"
    )
    if agg["runs_with_second_post"]:
        lines.append(
            f"  синтез не се е случил (върната е една версия дословно): "
            f"{agg['synthesis_skipped']} от {agg['runs_with_second_post']}"
        )
    lines.append(f"  отваря с факт: {agg['opens_with_fact']} от {agg['runs']}")
    lines.append(
        f"  механични находки на Критика (целта е 0): {agg['mechanical_findings_total']}"
    )

    if previous:
        lines.append("")
        lines.append(_render_comparison(agg, previous.get("aggregate") or {}))

    return "\n".join(lines)


def _render_comparison(current: dict[str, Any], previous: dict[str, Any]) -> str:
    """
    Разликата спрямо по-стар отчет. Само това е приемният критерий на Фаза 3 — абсолютните
    числа сами по себе си не значат нищо, защото метриката е прокси (виж metrics.py).
    """
    if not previous:
        return "Сравнение: предишният отчет не съдържа агрегат."

    lines = ["Спрямо предишния отчет:"]
    for stage, label in (("post_i", "Post I"), ("official", "предложение")):
        now, before = current.get(stage), previous.get(stage)
        if not now or not before:
            continue
        for key, name in (
            ("facts", "факти"),
            ("density", "плътност"),
            ("words", "думи"),
            ("empty_sentence_share", "празни"),
        ):
            delta = now[key] - before[key]
            sign = "+" if delta > 0 else ""
            lines.append(f"  {label:12} {name:10} {before[key]:>6} → {now[key]:>6}  ({sign}{round(delta, 1)})")
    for key, name in (
        ("mechanical_findings_total", "механични находки"),
        ("official_has_fewer_facts_than_post_i", "по-малко факти от Post I"),
    ):
        if key in previous:
            lines.append(f"  {name:24} {previous[key]} → {current.get(key)}")
    return "\n".join(lines)


def save(report: BenchReport, path: Path) -> Path:
    """Записва отчета като JSON — за по-късно сравнение през `--compare` (виж render/_render_comparison)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load(path: Path) -> dict[str, Any] | None:
    """Обратното на save() — None при липсващ или невалиден файл, никога изключение (CLI команда)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
