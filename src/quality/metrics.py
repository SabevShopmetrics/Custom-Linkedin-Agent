"""
Измерими характеристики на текста на един пост — без модел, без мрежа, само броене.

Защо съществува (план 11, Фаза 1 — виж docs/research-plans/research-plan-11-text-quality.md).
Дотогава единственото, което кодът знаеше за качеството на изхода, беше дължината в думи и
броят емоджи. Затова беше възможно веригата да произведе текст, по-беден от собствената си
първа чернова в 13 от 20 пускания, без нищо да го отбележи: и двата текста са „в норма".

Главната метрика тук НЕ е дължина, а **плътност**: колко собствени имена и числа има на
100 думи. Тя различава реален пост от генериран по-надеждно от всичко друго, което успяхме
да преброим — в 5-те дословни поста на клъстера почти всяко изречение носи име, число или
дата, а в генерираните се появяват изречения от вида „фокусирахме се върху ключови етапи от
подготовката", които биха стояли еднакво добре в пост на всяка друга организация.

ВАЖНО ограничение, което не бива да се забравя при четене на числата: това е ПРОКСИ, не
истина. Разпознаването на собствено име е евристика (главна буква не в началото на
изречение + низове в кавички + версални думи), не NER. Нарочно е проста и предвидима, за да
може да се сравнява „преди/след" — стойността ѝ е в разликата между два текста, измерени по
един и същи начин, не в абсолютното число. Затова план 11 изисква до метриката да върви и
човешка оценка, и при разминаване хората печелят.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass
from typing import Any

from src.brand.format_rules import MONTHS, count_words, extract_emoji

# Дума: последователност от букви (кирилица или латиница), допускаща тире и цифри вътре.
_WORD_RE = re.compile(r"[^\W\d_][\w\-']*", re.UNICODE)

# Число: 5, 2026, 30 000, 1 115, 12,5 — интервалът като разделител на хиляди е нарочно
# включен, защото българският текст го ползва („над 30 000 лв.").
_NUMBER_RE = re.compile(r"\d+(?:[   ]\d{3})*(?:[.,]\d+)?")

# Низ в български кавички: „Мост към бъдещето", „Сверяване на часовника". Почти винаги е
# име на програма, събитие или институция — тоест конкретност, дори когато главната буква
# стои в началото на изречение и евристиката по-долу не би я хванала.
_QUOTED_RE = re.compile(r"„([^“”\"]{2,60})[“”\"]")

# Съкращения, след които точката НЕ е край на изречение. Без този списък „д-р Норман" и
# „доц. Радева" губят името си — то се брои като начало на изречение.
_ABBREVIATIONS = ("д-р", "доц", "проф", "инж", "г", "гр", "бул", "ул", "лв", "ет", "стр", "т.н", "напр")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


@dataclass(frozen=True)
class TextMetrics:
    """Каквото кодът може да преброи в един готов пост."""

    words: int
    sentences: int
    proper_nouns: int
    numbers: int
    emoji: int
    # Абсолютен брой факти в поста (имена + числа). Нужен ДО плътността, а не вместо нея:
    # първото измерване на 20-те пускания показа, че плътността Post I → предложение почти
    # не се мени (медиана 10.1 → 10.2), докато абсолютният брой факти пада заедно с
    # дължината. Тоест веригата не разрежда текста — тя изхвърля цели факти. Метрика само
    # в проценти би отчела това като „без промяна".
    facts: int
    # (собствени имена + числа) / думи * 100
    density: float
    # дял на изреченията без нито едно име и нито едно число, в проценти. Пълнеж.
    empty_sentence_share: float
    opens_with_fact: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_numbers(text: str) -> list[str]:
    return _NUMBER_RE.findall(text)


def split_sentences(text: str) -> list[str]:
    """
    Изреченията, върху които се мери пълнежът. Всеки булет се брои като отделно изречение,
    защото в гласа на клъстера булетът НОСИ факта — булет без факт е точно дефектът, който
    търсим.
    """
    parts = [part.strip(" \t-•*") for part in _SENTENCE_SPLIT_RE.split(text)]
    return [part for part in parts if _WORD_RE.search(part)]


def _is_abbreviation_before(text: str, position: int) -> bool:
    """Точката преди `position` част от съкращение ли е (д-р, доц., бул.), не край на изречение."""
    fragment = text[:position].rstrip()
    if not fragment.endswith("."):
        return False
    tail = fragment[:-1]
    last_word = _WORD_RE.findall(tail)
    if not last_word:
        return False
    return last_word[-1].lower() in _ABBREVIATIONS


def find_proper_nouns(text: str) -> list[str]:
    """
    Евристика за собствено име: дума с главна буква, която НЕ е в началото на изречение,
    плюс версалните думи (ТЕХНОСТАРТ, CONNEXUS, AI) и всичко в български кавички.

    Съзнателно приета неточност: „Клъстерът" в началото на изречение не се брои, а
    „Варна" в средата се брои. Целта е сравнимост между два текста, не лексикална истина.
    """
    found: list[str] = []

    # Името в кавички се брои ЕДИН път като цяло. Без това „Сверяване на часовника" влиза
    # веднъж като низ и после още веднъж за всяка своя дума — тримесечно име тежи четири
    # пъти повече от еднословно, а плътността се надува тихо.
    quoted_spans: list[tuple[int, int]] = []
    for match in _QUOTED_RE.finditer(text):
        found.append(match.group(1).strip())
        quoted_spans.append(match.span())

    def inside_quotes(position: int) -> bool:
        return any(start <= position < end for start, end in quoted_spans)

    sentence_start = True
    for match in _WORD_RE.finditer(text):
        if inside_quotes(match.start()):
            continue
        word = match.group(0)
        before = text[: match.start()].rstrip()

        if not before:
            starts_sentence = True
        elif before[-1] in ".!?…":
            starts_sentence = not _is_abbreviation_before(text, match.start())
        elif before[-1] == "\n" or "\n" in text[len(before) : match.start()]:
            starts_sentence = True
        else:
            starts_sentence = False

        sentence_start = starts_sentence
        first = word[0]
        is_upper_word = len(word) > 1 and word.isupper()
        if is_upper_word or (first.isupper() and not sentence_start):
            found.append(word)

    return found


def _has_a_fact(fragment: str) -> bool:
    lowered = fragment.lower()
    if find_numbers(fragment):
        return True
    if any(month in lowered for month in MONTHS):
        return True
    return bool(find_proper_nouns(fragment))


def measure(text: str) -> TextMetrics:
    """Всички метрики за един пост. Празен текст дава нули, не изключение."""
    words = count_words(text)
    sentences = split_sentences(text)
    proper_nouns = find_proper_nouns(text)
    numbers = find_numbers(text)

    empty = [sentence for sentence in sentences if not _has_a_fact(sentence)]
    first_line = next((line for line in text.splitlines() if line.strip()), "")

    return TextMetrics(
        words=words,
        sentences=len(sentences),
        proper_nouns=len(proper_nouns),
        numbers=len(numbers),
        emoji=len(extract_emoji(text)),
        facts=len(proper_nouns) + len(numbers),
        density=round(100 * (len(proper_nouns) + len(numbers)) / words, 1) if words else 0.0,
        empty_sentence_share=round(100 * len(empty) / len(sentences), 1) if sentences else 0.0,
        opens_with_fact=_has_a_fact(first_line),
    )


@dataclass(frozen=True)
class ReferenceBand:
    """
    Границите, изведени от дословните постове на клъстера. Не са цел, която трябва да се
    улучи — те са мярката „как изглежда пост, който тези хора наистина са публикували".
    """

    sample_size: int
    words_min: int
    words_max: int
    words_median: float
    facts_median: float
    density_min: float
    density_median: float
    empty_sentence_share_median: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def reference_band(verbatim_examples: list[str]) -> ReferenceBand | None:
    """
    Референтната лента от `knowledge/examples/good/` — само дословните постове.

    Обобщенията НЕ влизат (виж src/brand/loader.py): те са 23–44 думи, а истинските постове
    103–170, и точно осредняването на двете популации роди грешното правило „60–150 думи"
    в knowledge/voice/tone.md, което план 11 ревизира.
    """
    if not verbatim_examples:
        return None

    measured = [measure(text) for text in verbatim_examples]
    words = [m.words for m in measured]
    densities = [m.density for m in measured]
    empties = [m.empty_sentence_share for m in measured]

    return ReferenceBand(
        sample_size=len(measured),
        words_min=min(words),
        words_max=max(words),
        words_median=round(statistics.median(words), 1),
        facts_median=round(statistics.median(m.facts for m in measured), 1),
        density_min=round(min(densities), 1),
        density_median=round(statistics.median(densities), 1),
        empty_sentence_share_median=round(statistics.median(empties), 1),
    )
