"""
Регресионен пазач за реален пропуск от 2026-08-10: в реален end-to-end тест Писателят
буквално вгради вътрешна бележка на Критика ("⚠️ GDPR предупреждение: ...") в текста на
самия пост — стигна чак до output/post.md. Не можем детерминирано да тестваме дали LLM ще
се подчини на инструкция, но можем да гарантираме, че самата инструкция не изчезне мълчаливо
при бъдещо редактиране на промпта.
"""

from src.pipeline.writer import SECOND_POST_SYSTEM_PROMPT_TEMPLATE


def test_second_post_prompt_explicitly_forbids_leaking_critic_notes_into_post_text():
    assert "ВЪТРЕШНИ" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE
    assert "GDPR предупреждение" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE  # конкретният наблюдаван пример
    assert "не се появяват" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE


def test_second_post_prompt_asks_for_a_second_version_not_a_patch():
    """От 2026-08-11: стъпка 5 пише втори пълноценен пост, не козметична поправка на
    първия — ако промптът се върне към "поправи само каквото е посочено", Консолидаторът
    получава два почти еднакви текста и синтезът губи смисъл."""
    assert "ВТОРА версия" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE
    assert "преструктурираш" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE


def test_draft_prompt_does_not_instruct_hashtags():
    """voice/tone.md: 'НЕ ползва хаштагове. Никога.' — по-старият промпт текст
    противоречиво казваше на Писателя да добавя хаштагове в края."""
    from src.pipeline.writer import DRAFT_SYSTEM_PROMPT_TEMPLATE

    assert "хаштагове в края" not in DRAFT_SYSTEM_PROMPT_TEMPLATE


# --------------------------------------------------------------- план 11, Фаза 0
# Писателят вече получава facts.yaml и правилото за споменаване на организации. Дотогава
# тези източници влизаха само в промпта на Критика — корпусът служеше, за да хване
# Писателя, че не е конкретен, но не и за да му даде с какво да бъде (находка 2.6).


def test_draft_prompt_carries_the_fact_sources_block():
    from src.pipeline.writer import DRAFT_SYSTEM_PROMPT_TEMPLATE

    assert "{sources_block}" in DRAFT_SYSTEM_PROMPT_TEMPLATE


def test_second_post_prompt_carries_the_fact_sources_block():
    """Вторият пост също трябва да може да върне факт, който първият е пропуснал."""
    assert "{sources_block}" in SECOND_POST_SYSTEM_PROMPT_TEMPLATE


def test_sources_block_includes_facts_yaml_when_available():
    from src.pipeline.writer import render_sources_block
    from tests.conftest import make_brand_knowledge

    knowledge = make_brand_knowledge(
        facts_available=True,
        facts={"organization": {"website": "https://ictclustervarna.com"}},
    )

    block = render_sources_block(knowledge)

    assert "ictclustervarna.com" in block
    assert "identity/facts.yaml" in block


def test_sources_block_says_facts_are_missing_instead_of_pretending():
    from src.pipeline.writer import render_sources_block
    from tests.conftest import make_brand_knowledge

    block = render_sources_block(make_brand_knowledge(facts_available=False, facts=None))

    assert "липсва" in block
    assert "брифът е единственият" in block.lower()


def test_sources_block_lists_only_members_with_confirmed_consent():
    """
    Обратното на списъка при Критика: Критикът вижда всички членове (за да хване
    неразрешено споменаване), Писателят вижда само разрешените. Име, което не е пред
    очите му, е име, което няма да сложи.
    """
    from src.pipeline.writer import render_sources_block
    from tests.conftest import make_brand_knowledge

    knowledge = make_brand_knowledge(
        members=[
            {"name": "Zariba ЕООД (Zariba)", "mention_allowed": True},
            {"name": "Тайна фирма ЕООД", "mention_allowed": None},
            {"name": "Друга фирма ЕООД", "mention_allowed": False},
        ]
    )

    block = render_sources_block(knowledge)

    assert "Zariba" in block
    assert "Тайна фирма" not in block
    assert "Друга фирма" not in block


def test_sources_block_keeps_the_brief_authoritative_when_no_member_is_mentionable():
    """
    Всичките 83 члена в members.yaml са с mention_allowed: null. Правилото не бива да е
    'не назовавай никого' — tone.md изисква поименна благодарност, а брифът е писан от
    човек, който отговаря за името в него. Забранено е само ДОБАВЯНЕТО на имена от корпуса.
    """
    from src.pipeline.writer import render_sources_block
    from tests.conftest import make_brand_knowledge

    block = render_sources_block(
        make_brand_knowledge(members=[{"name": "Фирма ЕООД", "mention_allowed": None}])
    )

    assert "име, което не е в брифа, не влиза в поста" in block
    assert "Фирма ЕООД" not in block


def test_draft_prompt_demands_a_fact_per_sentence():
    """Основната мярка за качество от план 11 е плътност на факти, не дължина."""
    from src.pipeline.writer import DRAFT_SYSTEM_PROMPT_TEMPLATE

    assert "Плътност" in DRAFT_SYSTEM_PROMPT_TEMPLATE
    assert "ключови етапи" in DRAFT_SYSTEM_PROMPT_TEMPLATE  # наблюдаваният празен израз
