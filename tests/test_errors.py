"""
Всяка грешка трябва да казва на човека какво да направи — не просто да носи
техническа подробност. Това е най-важната гаранция в целия проект (виж CLAUDE.md
и agent-bootstrap-prompt.md, раздел "ОБРАБОТКА НА ГРЕШКИ").
"""

import src.errors
from src.errors import (
    BriefingMissingError,
    BriefingNotFilledError,
    ImageTooLargeError,
    InvalidResponseError,
    ModelUnavailableError,
    NoPhotosWarning,
)


def test_briefing_missing_error_names_the_folder():
    err = BriefingMissingError("posts/2026-07-praktiki")
    assert "posts/2026-07-praktiki" in err.human_message
    assert "briefing.md" in err.human_message


def test_briefing_not_filled_error_tells_user_to_edit_it():
    err = BriefingNotFilledError("posts/test-3")
    assert "posts/test-3" in err.human_message
    assert "шаблон" in err.human_message


def test_model_unavailable_error_says_what_to_check():
    err = ModelUnavailableError("writer", "Qwen 3.5 Large", 4, detail="timeout")
    assert "Qwen 3.5 Large" in err.human_message
    assert "4 опита" in err.human_message
    assert "Open WebUI" in err.human_message
    assert err.detail == "timeout"


def test_invalid_response_error_mentions_model_and_role():
    err = InvalidResponseError("planner", "velocity/Qwen3.6-35B-A3B", 3)
    assert "planner" in err.human_message
    assert "velocity/Qwen3.6-35B-A3B" in err.human_message


def test_image_too_large_error_reports_sizes_in_kb():
    err = ImageTooLargeError("DSC001.JPG", 6_000_000, 4_000_000)
    assert "DSC001.JPG" in err.human_message
    assert "KB" in err.human_message


def test_there_is_no_error_for_text_the_critic_dislikes():
    """
    От 2026-08-11 (потребителска инструкция): Критикът не отхвърля постове, а произвежда
    дневен ред за втори пост — виж docs/technical/process-3.md. `NeedsHumanReviewError`
    е премахната и не бива да се връща под друго име: грешките в този модул са само за
    вход и инфраструктура, никога за преценка на качеството на текста.
    """
    assert not hasattr(src.errors, "NeedsHumanReviewError")

    judgement_words = ("отхвърл", "needs_human", "не е одобрен")
    for name in dir(src.errors):
        attribute = getattr(src.errors, name)
        if isinstance(attribute, type) and issubclass(attribute, src.errors.AgentError):
            docstring = (attribute.__doc__ or "").lower()
            assert not any(word in docstring for word in judgement_words), name


def test_no_photos_warning_is_not_an_exception():
    warning = NoPhotosWarning("posts/x")
    assert not isinstance(warning, Exception)
    assert "photos/" in warning.human_message
    assert "posts/x" in warning.human_message
