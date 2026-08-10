"""
run.log трябва да съществува след всяко пускане (дори при спиране преди output/ да бъде
създадена) и да натрупва история между пускания, не да я трие.
"""

from src.logging_utils import setup_run_logger


def test_setup_run_logger_creates_log_file_in_post_dir(tmp_path):
    post_dir = tmp_path / "post"

    logger = setup_run_logger(post_dir)
    logger.info("тестово съобщение")

    log_path = post_dir / "run.log"
    assert log_path.exists()
    assert "тестово съобщение" in log_path.read_text(encoding="utf-8")


def test_setup_run_logger_appends_across_multiple_runs(tmp_path):
    post_dir = tmp_path / "post"

    setup_run_logger(post_dir).info("първо пускане")
    setup_run_logger(post_dir).info("второ пускане")

    content = (post_dir / "run.log").read_text(encoding="utf-8")
    assert "първо пускане" in content
    assert "второ пускане" in content


def test_setup_run_logger_console_handler_defaults_to_warning_level(tmp_path):
    logger = setup_run_logger(tmp_path / "post")
    console_handlers = [h for h in logger.handlers if h.__class__.__name__ == "StreamHandler"]
    assert len(console_handlers) == 1
    assert console_handlers[0].level == 30  # logging.WARNING
