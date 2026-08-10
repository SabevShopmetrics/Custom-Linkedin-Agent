"""
Всяка грешка трябва да казва на човека какво да направи — не просто да носи
техническа подробност. Това е най-важната гаранция в целия проект (виж CLAUDE.md
и agent-bootstrap-prompt.md, раздел "ОБРАБОТКА НА ГРЕШКИ").
"""

from src.errors import (
    BriefingMissingError,
    BriefingNotFilledError,
    ImageTooLargeError,
    InvalidResponseError,
    ModelUnavailableError,
    NeedsHumanReviewError,
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


def test_needs_human_review_error_immediate_mentions_no_rewrite_attempt():
    err = NeedsHumanReviewError("posts/x", ["критикува конкурент — rules/forbidden.md"], immediate=True)
    assert "needs_human" in err.human_message
    assert "критикува конкурент" in err.human_message
    assert "без опит за автоматично пренаписване" in err.human_message


def test_needs_human_review_error_after_retry_mentions_revision():
    err = NeedsHumanReviewError("posts/x", ["все още грешен тон"], immediate=False)
    assert "needs_human" in err.human_message
    assert "ревизия" in err.human_message
    assert "все още грешен тон" in err.human_message


def test_no_photos_warning_is_not_an_exception():
    warning = NoPhotosWarning("posts/x")
    assert not isinstance(warning, Exception)
    assert "photos/" in warning.human_message
    assert "posts/x" in warning.human_message
