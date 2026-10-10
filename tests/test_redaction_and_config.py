"""Secret handling: redaction, settings representation, error messages."""

from __future__ import annotations

import logging

from config import Settings
from security.errors import to_user_message
from security.redaction import RedactingFilter, redact, register_secrets


def test_redacts_connection_strings_and_keys():
    text = "postgresql+psycopg://powell_agent_ro:S3cret!@localhost:5432/db password=hunter2 sk-abcdefghijklmnop"
    out = redact(text)
    assert "S3cret!" not in out and "hunter2" not in out and "abcdefghijklmnop" not in out


def test_registered_secret_values_redacted():
    register_secrets(["MyVerySpecialPw"])
    assert "MyVerySpecialPw" not in redact("login failed for MyVerySpecialPw")


def test_log_filter_redacts(caplog):
    register_secrets(["TopSecretValue"])
    logger = logging.getLogger("redaction_test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("connecting with %s", "TopSecretValue")
    assert "TopSecretValue" not in caplog.text


def test_settings_repr_hides_secrets():
    s = Settings(_env_file=None, pg_password="pw-should-hide", openai_api_key="sk-shouldhide12345")
    assert "pw-should-hide" not in repr(s) and "sk-shouldhide12345" not in repr(s)
    assert s.approved_schemas == ["salesdata", "data_science", "gis", "public", "training"]


def test_user_message_has_no_stack_or_secret():
    register_secrets(["pw123456"])
    msg = to_user_message(RuntimeError("connection to server failed: password=pw123456\nTraceback ..."), "query")
    assert "pw123456" not in msg and "Traceback" not in msg


def test_env_file_wins_over_environment_and_keys_are_cleaned(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('OPENAI_API_KEY=" sk-proj-NEWKEY1234567890 "\nPG_PASSWORD=Agent2026ro  \n', encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-OLDKEY-H6YA")
    s = Settings(_env_file=str(env))
    assert s.openai_api_key.get_secret_value() == "sk-proj-NEWKEY1234567890"
    assert s.pg_password.get_secret_value() == "Agent2026ro"


def test_console_log_has_no_traceback(capsys, tmp_path):
    import logging

    from security.redaction import _ConsoleFormatter

    handler = logging.StreamHandler()
    handler.setFormatter(_ConsoleFormatter("%(levelname)s %(message)s"))
    logger = logging.getLogger("console_test")
    logger.addHandler(handler)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("database connection failed")
    err = capsys.readouterr().err
    assert "database connection failed" in err and "Traceback" not in err


def test_spreadsheet_error_values_get_a_clear_message():
    msg = to_user_message(RuntimeError('invalid input syntax for type double precision: "#NAME?"'), "query")
    assert "#NAME?" in msg and "spreadsheet" in msg


def test_password_gate_logic():
    pytest = __import__("pytest")
    pytest.importorskip("streamlit")
    from components.branding import password_ok

    s = Settings(_env_file=None, app_password="correct horse")
    assert password_ok(s, "correct horse") and not password_ok(s, "wrong")
    assert not password_ok(Settings(_env_file=None, app_password=""), "")
    assert "correct horse" not in repr(s)
