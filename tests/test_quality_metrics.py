"""
Метриката от план 11, Фаза 1. Тук се пази ПРЕДВИДИМОСТТА ѝ, не абсолютната ѝ точност:
разпознаването на собствено име е евристика, а стойността на числата е в сравнението
между два текста, измерени по един и същи начин. Затова тестовете фиксират поведението,
което би направило сравнението невалидно, ако се промени незабелязано.
"""

from __future__ import annotations

from src.quality.metrics import (
    find_numbers,
    find_proper_nouns,
    measure,
    reference_band,
    split_sentences,
)

REAL_POST = """\
На 11 юни проведохме петото издание на „Сверяване на часовника" 2026 на ИКТ Клъстер – Варна.
Специален гост на събитието беше съоснователят на Siri д-р Норман Уинарски.
"""

FILLER_POST = """\
Срещата бе фокусирана върху ключови етапи от подготовката.
Целта е да осигурим максимална полза за всички участници чрез добре структуриран процес.
"""


def test_proper_noun_at_sentence_start_is_not_counted_as_a_name():
    """
    Иначе всяко изречение би започвало със „собствено име" и плътността на всеки текст би
    се надувала еднакво — метриката губи разграничителна сила.
    """
    assert find_proper_nouns("Клъстерът проведе среща.") == []
    assert "Варна" in find_proper_nouns("Срещата се проведе във Варна.")


def test_name_after_an_abbreviation_is_still_a_name():
    """„д-р Норман" — точката в съкращението не е край на изречение."""
    names = find_proper_nouns("Гост беше д-р Норман Уинарски.")

    assert "Норман" in names
    assert "Уинарски" in names


def test_quoted_name_counts_once_not_once_per_word():
    """Триизмерно име в кавички не бива да тежи четири пъти повече от еднословно."""
    names = find_proper_nouns('Петото издание на „Сверяване на часовника" 2026.')

    assert names.count("Сверяване на часовника") == 1
    assert "Сверяване" not in names
    assert "часовника" not in names


def test_all_caps_names_are_counted_even_at_sentence_start():
    assert "CONNEXUS" in find_proper_nouns("CONNEXUS се провежда всяка година.")


def test_numbers_include_thousands_separated_by_space():
    """Българският текст пише „над 30 000 лв." — две числа вместо едно изкривява плътността."""
    assert find_numbers("Събрахме над 30 000 лв. от 12 участници.") == ["30 000", "12"]


def test_each_bullet_counts_as_a_sentence():
    """В гласа на клъстера булетът НОСИ факта — булет без факт е точно търсеният дефект."""
    text = "Обсъдихме:\n🔹 първа тема\n🔹 втора тема"

    assert len(split_sentences(text)) == 3


def test_filler_text_scores_worse_than_a_real_post_on_all_three_metrics():
    """
    Основното твърдение на цялата метрика. Ако този тест падне, сравненията в bench вече
    не значат каквото твърдят.
    """
    real = measure(REAL_POST)
    filler = measure(FILLER_POST)

    assert real.facts > filler.facts
    assert real.density > filler.density
    assert real.empty_sentence_share < filler.empty_sentence_share


def test_empty_sentence_share_flags_sentences_without_a_single_fact():
    metrics = measure(FILLER_POST)

    assert metrics.empty_sentence_share == 100.0
    assert metrics.opens_with_fact is False


def test_opens_with_fact_accepts_a_month_name_without_a_number():
    assert measure("През септември стартира програмата.").opens_with_fact is True


def test_measure_of_empty_text_is_zeros_not_an_exception():
    metrics = measure("")

    assert metrics.words == 0
    assert metrics.density == 0.0
    assert metrics.empty_sentence_share == 0.0


def test_reference_band_is_none_without_verbatim_examples():
    """Празен корпус не бива да произвежда измислена лента, срещу която да се сравнява."""
    assert reference_band([]) is None


def test_reference_band_summarizes_the_verbatim_corpus():
    band = reference_band([REAL_POST, FILLER_POST])

    assert band.sample_size == 2
    assert band.words_min <= band.words_median <= band.words_max
