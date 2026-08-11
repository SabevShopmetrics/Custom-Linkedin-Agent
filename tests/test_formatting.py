"""
Механичните правила: четене от корпуса (src/brand/format_rules.py) и налагане върху
готовото предложение (src/pipeline/formatting.py).

Поводът е реален. На 2026-08-11 Критикът флагна 📍 и 📅 като неразрешени и изброи
„разрешения списък" по памет — с един символ (✨) в него, който го НЯМА в корпуса. В същия
пост моделът написа адрес `https://ictcluster-varna.org`, който не съществува, при положение
че истинският (`ictclustervarna.com`) е във facts.yaml, в същия промпт. Двата случая имат
едно общо: правило, което се брои и сравнява, не бива да зависи от паметта на модел.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.brand.format_rules import FormatRules, load_format_rules, parse_format_rules
from src.pipeline.formatting import LINK_PLACEHOLDER, enforce

KNOWLEDGE_DIR = Path("knowledge")

TONE_SAMPLE = """\
# Тон и стил

- Emoji тематични, 3–6 на пост: 🙏 благодарност, 🤝 партньорство, 📖/🔗 препратка,
  ✅ акценти, 🔹 теми, 👏 поздравления. Никога декоративни или в излишък.
- НЕ ползва хаштагове. Никога.
- Отваряне с конкретен факт: число, дата или място. Не с общо въведение.
- Дължина: 60–150 думи. Гъсто с факти, не разтегнато с общи фрази.
"""

FORMATS_SAMPLE = """\
# Формати

- Списък с акценти в средата на поста — маркиран с ✅ или 🔹, не с тире или звезда.
"""

FACTS_SAMPLE = {
    "organization": {"website": "ictclustervarna.com"},
    "channels": {"linkedin_url": "https://bg.linkedin.com/company/ict-cluster-varna"},
}


def sample_rules() -> FormatRules:
    return parse_format_rules(TONE_SAMPLE, FORMATS_SAMPLE, FACTS_SAMPLE)


# --------------------------------------------------------------------------- четене на правилата


def test_multiline_bullet_is_read_whole():
    """Емоджи правилото в tone.md се пренася на втори ред. Ред-по-ред четене изпускаше 👏 и
    така тихо забраняваше разрешен символ."""
    rules = sample_rules()

    assert rules.allowed_emoji == frozenset({"🙏", "🤝", "📖", "🔗", "✅", "🔹", "👏"})


def test_emoji_count_range_and_length_are_read_from_the_corpus():
    rules = sample_rules()

    assert (rules.emoji_min, rules.emoji_max) == (3, 6)
    assert (rules.words_min, rules.words_max) == (60, 150)
    assert rules.hashtags_forbidden is True
    assert rules.opening_needs_fact is True


def test_known_hosts_come_from_facts_yaml():
    rules = sample_rules()

    assert rules.known_hosts() == ("ictclustervarna.com", "bg.linkedin.com")


def test_missing_rules_are_reported_not_invented():
    """Липсващо правило не се налага. Празен корпус значи нула механични проверки, не
    подразбиращ се списък от кода."""
    rules = parse_format_rules("", "", None)

    assert rules.allowed_emoji == frozenset()
    assert rules.hashtags_forbidden is False
    assert rules.words_min is None
    assert len(rules.unparsed) == 5  # емоджи, хаштагове, отваряне, дължина, URL
    assert not rules.enforces_emoji


def test_real_corpus_parses_into_enforceable_rules():
    """Пази връзката код ↔ корпус. Ако клъстерът преформулира tone.md така, че правилата
    спрат да се четат, този тест пада — вместо мълчаливо да спре налагането им."""
    facts = yaml.safe_load((KNOWLEDGE_DIR / "identity/facts.yaml").read_text(encoding="utf-8"))
    rules = load_format_rules(KNOWLEDGE_DIR, facts)

    assert "✨" not in rules.allowed_emoji  # символът, който моделът си измисли
    assert {"🙏", "🤝", "📖", "🔗", "✅", "🔹", "👏"} <= rules.allowed_emoji
    assert rules.hashtags_forbidden is True
    assert rules.emoji_max == 6
    assert "ictclustervarna.com" in rules.known_hosts()
    assert rules.unparsed == ()


# --------------------------------------------------------------------------- налагане


def test_disallowed_emoji_are_removed():
    result = enforce("На 5 август 2026 г. се събрахме. 📍 Дойдоха 12 души. 📅 🙏", sample_rules())

    assert "📍" not in result.text
    assert "📅" not in result.text
    assert "🙏" in result.text
    assert any("неразрешени емоджи" in fix for fix in result.fixes)


def test_hashtags_are_removed():
    result = enforce("12 участници на срещата. #ИКТ #Варна", sample_rules())

    assert "#" not in result.text
    assert any("хаштагове" in fix for fix in result.fixes)


def test_excess_emoji_are_trimmed_from_the_end():
    """Реже отзад: последните емоджи са декоративни, първите маркират списъка с акценти."""
    text = "12 души на 5 август.\n✅ едно\n🔹 две\n✅ три\n🙏 благодарим\n🤝 партньори\n👏 браво\n📖 линк"
    result = enforce(text, sample_rules())

    from src.brand.format_rules import extract_emoji

    assert len(extract_emoji(result.text)) == 6
    assert result.text.startswith("12 души")
    assert "✅ едно" in result.text  # структурата в началото остава
    assert any("надбройни" in fix for fix in result.fixes)


def test_invented_url_is_replaced_with_placeholder():
    result = enforce(
        "На 5 август 2026 се събрахме. Вижте повече: https://ictcluster-varna.org",
        sample_rules(),
        brief_text="Среща на 5 август 2026.",
    )

    assert "ictcluster-varna.org" not in result.text
    assert LINK_PLACEHOLDER in result.text
    assert any("измислени от модела" in fix for fix in result.fixes)


def test_known_and_briefed_urls_survive():
    rules = sample_rules()
    text = (
        "12 души на 5 август. Сайт: https://ictclustervarna.com/programa "
        "и регистрация: https://forms.gle/abc123"
    )

    result = enforce(text, rules, brief_text="Регистрация: https://forms.gle/abc123")

    assert "ictclustervarna.com/programa" in result.text  # познат хост от facts.yaml
    assert "forms.gle/abc123" in result.text  # подаден в брифа
    assert result.fixes == []


def test_word_count_outside_the_range_is_a_note_not_a_fix():
    result = enforce("5 август. Кратко.", sample_rules())

    assert any("под минимума" in note for note in result.notes)
    assert result.text == "5 август. Кратко."  # кодът не дописва текст


def test_too_few_emoji_is_a_note():
    result = enforce("На 5 август 2026 г. се събрахме 12 души. 🙏", sample_rules())

    assert any("под 3" in note for note in result.notes)


def test_opening_without_a_fact_is_flagged():
    result = enforce("Радваме се да съобщим нещо хубаво. 🙏 🤝 ✅", sample_rules())

    assert any("Първият ред" in note for note in result.notes)


def test_opening_with_a_month_name_passes():
    result = enforce("Август донесе среща с фирмите членове. 🙏 🤝 ✅", sample_rules())

    assert not any("Първият ред" in note for note in result.notes)


def test_enforce_is_idempotent():
    """Възобновяемост: при повторно пускане output/post.md вече е чист и второто минаване
    не бива да променя нищо, нито да отчита поправки."""
    first = enforce("12 души. 📍 #хаштаг https://измислен.бг 🙏 🤝 ✅", sample_rules())
    second = enforce(first.text, sample_rules())

    assert second.text == first.text
    assert second.fixes == []


def test_nothing_is_enforced_when_the_corpus_is_silent():
    empty = parse_format_rules("", "", None)

    result = enforce("Пост с #хаштаг и 📍 емоджи.", empty)

    assert "#хаштаг" in result.text
    assert "📍" in result.text
    assert result.fixes == []
