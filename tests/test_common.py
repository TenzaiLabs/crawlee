from __future__ import annotations

from app.common import sanitize_log_value


def test_sanitize_log_value_escapes_line_breaks() -> None:
    assert sanitize_log_value("job-1\r\nINFO forged\nline\rmore") == (
        "job-1\\r\\nINFO forged\\nline\\rmore"
    )


def test_sanitize_log_value_redacts_sensitive_pairs() -> None:
    assert sanitize_log_value("token=secret password: hunter2") == (
        "token=[REDACTED] password: [REDACTED]"
    )
