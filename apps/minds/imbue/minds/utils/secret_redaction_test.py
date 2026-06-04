from imbue.minds.utils.secret_redaction import REDACTED_PLACEHOLDER
from imbue.minds.utils.secret_redaction import redact_secret_flag_values


def test_redact_secret_flag_values_masks_space_separated_value() -> None:
    command = ["mngr", "pool", "list", "--database-url", "postgres://u:p@host/db"]

    redacted = redact_secret_flag_values(command, secret_bearing_flags=("--database-url",))

    assert "postgres://u:p@host/db" not in " ".join(redacted)
    assert redacted == ["mngr", "pool", "list", "--database-url", REDACTED_PLACEHOLDER]


def test_redact_secret_flag_values_masks_joined_equals_form() -> None:
    command = ["mngr", "pool", "list", "--database-url=postgres://u:p@host/db"]

    redacted = redact_secret_flag_values(command, secret_bearing_flags=("--database-url",))

    assert "postgres://u:p@host/db" not in " ".join(redacted)
    assert redacted == ["mngr", "pool", "list", f"--database-url={REDACTED_PLACEHOLDER}"]


def test_redact_secret_flag_values_masks_every_occurrence() -> None:
    command = ["x", "--token", "aaa", "y", "--token", "bbb"]

    redacted = redact_secret_flag_values(command, secret_bearing_flags=("--token",))

    assert "aaa" not in redacted
    assert "bbb" not in redacted
    assert redacted == ["x", "--token", REDACTED_PLACEHOLDER, "y", "--token", REDACTED_PLACEHOLDER]


def test_redact_secret_flag_values_masks_multiple_distinct_flags() -> None:
    command = ["cmd", "--database-url", "dsn", "--preauth-cookie", "cookie"]

    redacted = redact_secret_flag_values(command, secret_bearing_flags=("--database-url", "--preauth-cookie"))

    assert "dsn" not in redacted
    assert "cookie" not in redacted
    assert redacted == ["cmd", "--database-url", REDACTED_PLACEHOLDER, "--preauth-cookie", REDACTED_PLACEHOLDER]


def test_redact_secret_flag_values_is_noop_when_flag_absent() -> None:
    command = ["mngr", "pool", "list"]

    assert redact_secret_flag_values(command, secret_bearing_flags=("--database-url",)) == command


def test_redact_secret_flag_values_handles_flag_as_final_token() -> None:
    # A dangling secret flag with no following value must not raise.
    command = ["mngr", "pool", "list", "--database-url"]

    assert redact_secret_flag_values(command, secret_bearing_flags=("--database-url",)) == command


def test_redact_secret_flag_values_does_not_mutate_input() -> None:
    command = ["mngr", "--database-url", "secret-dsn"]

    redact_secret_flag_values(command, secret_bearing_flags=("--database-url",))

    assert command == ["mngr", "--database-url", "secret-dsn"]
