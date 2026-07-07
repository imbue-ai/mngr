from imbue.minds.utils.secret_redaction import REDACTED_PLACEHOLDER
from imbue.minds.utils.secret_redaction import redact_secret_env_assignments
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


def test_redact_secret_env_assignments_masks_value_keeping_name() -> None:
    command = [
        "mngr",
        "create",
        "--host-env",
        "LATCHKEY_GATEWAY=http://127.0.0.1:1989",
        "--host-env",
        "LATCHKEY_GATEWAY_PASSWORD=deadbeefsecret",
    ]

    redacted = redact_secret_env_assignments(command, secret_env_var_names=("LATCHKEY_GATEWAY_PASSWORD",))

    assert "deadbeefsecret" not in " ".join(redacted)
    # The non-secret gateway URL and the flag / variable name are preserved.
    assert redacted == [
        "mngr",
        "create",
        "--host-env",
        "LATCHKEY_GATEWAY=http://127.0.0.1:1989",
        "--host-env",
        f"LATCHKEY_GATEWAY_PASSWORD={REDACTED_PLACEHOLDER}",
    ]


def test_redact_secret_env_assignments_masks_every_secret_name() -> None:
    command = [
        "--host-env",
        "LATCHKEY_GATEWAY_PASSWORD=pw",
        "--host-env",
        "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE=eyJ.jwt.sig",
    ]

    redacted = redact_secret_env_assignments(
        command,
        secret_env_var_names=("LATCHKEY_GATEWAY_PASSWORD", "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"),
    )

    assert "pw" not in redacted
    assert "eyJ.jwt.sig" not in " ".join(redacted)
    assert redacted == [
        "--host-env",
        f"LATCHKEY_GATEWAY_PASSWORD={REDACTED_PLACEHOLDER}",
        "--host-env",
        f"LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE={REDACTED_PLACEHOLDER}",
    ]


def test_redact_secret_env_assignments_preserves_value_with_embedded_equals() -> None:
    # Only the first ``=`` splits name from value, so a value that itself
    # contains ``=`` (e.g. base64 padding in a JWT) is masked wholesale.
    command = ["MINDS_API_KEY=abc=def=="]

    redacted = redact_secret_env_assignments(command, secret_env_var_names=("MINDS_API_KEY",))

    assert redacted == [f"MINDS_API_KEY={REDACTED_PLACEHOLDER}"]


def test_redact_secret_env_assignments_is_noop_for_non_secret_names() -> None:
    command = ["mngr", "--host-env", "LATCHKEY_GATEWAY=http://127.0.0.1:1989", "positional"]

    assert redact_secret_env_assignments(command, secret_env_var_names=("LATCHKEY_GATEWAY_PASSWORD",)) == command


def test_redact_secret_env_assignments_ignores_tokens_without_equals() -> None:
    # A bare token equal to a secret name (no ``=``) is not an assignment and
    # must be left untouched.
    command = ["LATCHKEY_GATEWAY_PASSWORD", "--flag"]

    assert redact_secret_env_assignments(command, secret_env_var_names=("LATCHKEY_GATEWAY_PASSWORD",)) == command


def test_redact_secret_env_assignments_does_not_mutate_input() -> None:
    command = ["--host-env", "LATCHKEY_GATEWAY_PASSWORD=pw"]

    redact_secret_env_assignments(command, secret_env_var_names=("LATCHKEY_GATEWAY_PASSWORD",))

    assert command == ["--host-env", "LATCHKEY_GATEWAY_PASSWORD=pw"]
