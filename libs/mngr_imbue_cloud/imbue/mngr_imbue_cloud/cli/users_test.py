import json

from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.users import users


def test_users_group_lists_subcommands() -> None:
    result = CliRunner().invoke(users, ["--help"])
    assert result.exit_code == 0
    for name in ("show", "resolve", "profile"):
        assert name in result.output


def test_profile_needs_no_account_and_prints_the_connectors_public_profile(
    local_connector_stub: tuple[str, list[str]],
) -> None:
    """The public read runs without a session: only the connector URL is needed."""
    connector_url, served_paths = local_connector_stub

    result = CliRunner().invoke(users, ["profile", "user-1", "--connector-url", connector_url])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "user_id": "user-1",
        "display_name": "Alice",
        "profile_picture_url": f"{connector_url}/users/user-1/profile-picture/abc",
    }
    assert served_paths == ["/users/user-1/profile"]


def test_resolve_help_documents_the_error_codes() -> None:
    result = CliRunner().invoke(users, ["resolve", "--help"])
    assert result.exit_code == 0
    assert "EMAIL" in result.output
    assert "user_not_found" in result.output
    assert "rate_limited" in result.output
