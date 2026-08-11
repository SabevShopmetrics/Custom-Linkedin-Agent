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
