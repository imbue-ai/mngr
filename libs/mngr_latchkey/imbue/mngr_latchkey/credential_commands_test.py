import pytest

from imbue.mngr_latchkey.credential_commands import CredentialCommandError
from imbue.mngr_latchkey.credential_commands import CredentialCommandParameter
from imbue.mngr_latchkey.credential_commands import build_credential_command_argv
from imbue.mngr_latchkey.credential_commands import describe_credential_command_failure
from imbue.mngr_latchkey.credential_commands import fallback_set_credentials_example
from imbue.mngr_latchkey.credential_commands import parse_credential_command_example


def test_parse_credential_command_example_splits_positional_placeholders() -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl aws <access-key-id> <secret-access-key>")

    assert parsed.argv_template == ("auth", "set-nocurl", "aws", "<access-key-id>", "<secret-access-key>")
    assert parsed.parameters == (
        CredentialCommandParameter(name="access-key-id", label="Access key id"),
        CredentialCommandParameter(name="secret-access-key", label="Secret access key"),
    )


def test_parse_credential_command_example_finds_a_placeholder_inside_a_quoted_argument() -> None:
    parsed = parse_credential_command_example('latchkey auth set slack -H "Authorization: Bearer <token>"')

    assert parsed.argv_template == ("auth", "set", "slack", "-H", "Authorization: Bearer <token>")
    assert parsed.parameters == (CredentialCommandParameter(name="token", label="Token"),)


def test_parse_credential_command_example_deduplicates_repeated_placeholders() -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl dup <api_key> <api_key>")

    assert parsed.parameters == (CredentialCommandParameter(name="api_key", label="Api key"),)


def test_parse_credential_command_example_accepts_an_absolute_binary_path() -> None:
    parsed = parse_credential_command_example("/opt/minds/bin/latchkey auth set-nocurl aws <key> <secret>")

    assert parsed.argv_template == ("auth", "set-nocurl", "aws", "<key>", "<secret>")


def test_parse_credential_command_example_rejects_a_command_without_placeholders() -> None:
    with pytest.raises(CredentialCommandError, match="no <...> parameters"):
        parse_credential_command_example("latchkey auth set slack --from-keychain")


def test_parse_credential_command_example_rejects_a_non_latchkey_command() -> None:
    with pytest.raises(CredentialCommandError, match="does not invoke latchkey"):
        parse_credential_command_example("aws configure set aws_access_key_id <key>")


def test_parse_credential_command_example_rejects_an_empty_command() -> None:
    with pytest.raises(CredentialCommandError, match="no command"):
        parse_credential_command_example("   ")


def test_parse_credential_command_example_rejects_unbalanced_quoting() -> None:
    with pytest.raises(CredentialCommandError, match="could not be parsed"):
        parse_credential_command_example('latchkey auth set slack -H "Authorization: Bearer <token>')


def test_build_credential_command_argv_substitutes_values_and_pins_the_account() -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl aws <access-key-id> <secret-access-key>")

    argv = build_credential_command_argv(
        parsed,
        {"access-key-id": "AKIA-72f1", "secret-access-key": "  shh-91ba  "},
        "alice@example.invalid",
    )

    assert argv == (
        "--account",
        "alice@example.invalid",
        "auth",
        "set-nocurl",
        "aws",
        "AKIA-72f1",
        "shh-91ba",
    )


def test_build_credential_command_argv_substitutes_inside_a_quoted_argument() -> None:
    parsed = parse_credential_command_example('latchkey auth set slack -H "Authorization: Bearer <token>"')

    argv = build_credential_command_argv(parsed, {"token": "xoxb-4471"}, "")

    assert argv == ("--account", "", "auth", "set", "slack", "-H", "Authorization: Bearer xoxb-4471")


def test_build_credential_command_argv_substitutes_every_occurrence_of_a_placeholder() -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl dup <api_key> <api_key>")

    argv = build_credential_command_argv(parsed, {"api_key": "k-8823"}, "")

    assert argv == ("--account", "", "auth", "set-nocurl", "dup", "k-8823", "k-8823")


def test_build_credential_command_argv_keeps_a_value_that_looks_like_a_placeholder_verbatim() -> None:
    """A pasted value is substituted, never re-scanned for placeholders."""
    parsed = parse_credential_command_example("latchkey auth set-nocurl aws <access-key-id> <secret-access-key>")

    argv = build_credential_command_argv(
        parsed,
        {"access-key-id": "<secret-access-key>", "secret-access-key": "shh-91ba"},
        "",
    )

    assert argv[-2:] == ("<secret-access-key>", "shh-91ba")


@pytest.mark.parametrize("blank_value", ["", "   "])
def test_build_credential_command_argv_rejects_a_blank_value(blank_value: str) -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl aws <access-key-id> <secret-access-key>")

    with pytest.raises(CredentialCommandError, match="Secret access key"):
        build_credential_command_argv(
            parsed,
            {"access-key-id": "AKIA-72f1", "secret-access-key": blank_value},
            "",
        )


def test_build_credential_command_argv_rejects_a_missing_value() -> None:
    parsed = parse_credential_command_example("latchkey auth set-nocurl aws <access-key-id> <secret-access-key>")

    with pytest.raises(CredentialCommandError, match="no value was supplied"):
        build_credential_command_argv(parsed, {"access-key-id": "AKIA-72f1"}, "")


# Verbatim from latchkey 3.3.0's ``Aws.getCredentialsNoCurl``: the explanation
# is worth showing, the example line prints the placeholder rather than a value.
_AWS_SHAPE_ERROR: str = (
    "Error: The provided access key ID doesn't look like an AWS access key ID "
    "(expected to start with AKIA or ASIA).\n"
    "Example: <access-key-id>"
)


def test_describe_credential_command_failure_keeps_the_explanation_and_drops_the_example() -> None:
    assert describe_credential_command_failure(_AWS_SHAPE_ERROR) == (
        "The provided access key ID doesn't look like an AWS access key ID (expected to start with AKIA or ASIA)."
    )


def test_describe_credential_command_failure_drops_an_example_that_is_a_terminal_command() -> None:
    detail = (
        "Error: Expected exactly two arguments: <access-key-id> <secret-access-key>.\n"
        "Example: latchkey auth set-nocurl aws <access-key-id> <secret-access-key>"
    )

    described = describe_credential_command_failure(detail)

    assert described == "Expected exactly two arguments: <access-key-id> <secret-access-key>."
    assert "latchkey" not in described


def test_describe_credential_command_failure_drops_stack_frames_from_a_crash() -> None:
    detail = (
        "file:///opt/latchkey/dist/src/apiCredentials/store.js:73\n"
        "ApiCredentialStoreError: Failed to write credential store: Invalid key length\n"
        "    at ApiCredentialStore.saveStoreData (file:///opt/latchkey/store.js:73:19)\n"
        "    at ApiCredentialStore.save (file:///opt/latchkey/store.js:123:14)"
    )

    assert describe_credential_command_failure(detail) == (
        "ApiCredentialStoreError: Failed to write credential store: Invalid key length"
    )


def test_describe_credential_command_failure_truncates_a_crash_dump() -> None:
    described = describe_credential_command_failure("boom " * 200)

    assert described.endswith("...")
    assert len(described) <= 303


def test_describe_credential_command_failure_is_empty_when_only_usage_lines_remain() -> None:
    assert describe_credential_command_failure("Usage: latchkey auth set <service>\n\n") == ""


def test_fallback_set_credentials_example_renders_the_service_header_as_one_token_input() -> None:
    bearer = fallback_set_credentials_example("custom_https_api_example_com", None)
    assert bearer == "latchkey auth set custom_https_api_example_com -H 'Authorization: Bearer <token>'"
    keyed = fallback_set_credentials_example("custom_https_api_example_com", "X-Api-Key: {token}")
    assert keyed == "latchkey auth set custom_https_api_example_com -H 'X-Api-Key: <token>'"
    # The existing form builder turns either into the same single labelled input, and
    # the filled command carries the header the service actually takes.
    parsed = parse_credential_command_example(keyed)
    assert [parameter.name for parameter in parsed.parameters] == ["token"]
    argv = build_credential_command_argv(parsed, {"token": "k-1"}, "")
    assert argv == ("--account", "", "auth", "set", "custom_https_api_example_com", "-H", "X-Api-Key: k-1")


@pytest.mark.parametrize(
    "header",
    ['Cookie: session="{token}"; theme=dark', "X-Sig: {token}\\v1", "X-Note: it's {token}"],
)
def test_a_header_with_shell_characters_survives_the_round_trip_into_argv(header: str) -> None:
    # The example is shlex-split before it runs, so a quote or backslash the header
    # carries must come back exactly, or the gateway would send a different header
    # than the dialog showed.
    parsed = parse_credential_command_example(fallback_set_credentials_example("custom_x", header))
    argv = build_credential_command_argv(parsed, {"token": "k-1"}, "")
    assert argv[-1] == header.replace("{token}", "k-1")
