from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.shares import _share_to_json
from imbue.mngr_imbue_cloud.cli.shares import shares
from imbue.mngr_imbue_cloud.wire_types import ShareInfo


def test_shares_group_lists_subcommands() -> None:
    result = CliRunner().invoke(shares, ["--help"])
    assert result.exit_code == 0
    for name in (
        "create",
        "delete",
        "status",
        "list",
        "relays",
        "set-grantees",
        "push-grants",
        "invite",
        "invitation-outcomes",
    ):
        assert name in result.output


def test_create_help_documents_arguments() -> None:
    result = CliRunner().invoke(shares, ["create", "--help"])
    assert result.exit_code == 0
    assert "HOST_ID" in result.output
    assert "--account" in result.output
    assert "--preferred-region" in result.output


def test_create_rejects_a_malformed_workspace_id_before_any_network_call() -> None:
    # A machine id where the workspace's identity belongs is the exact mixup
    # the WorkspaceId type exists to catch; the CLI fails with its JSON error
    # shape without touching the session store or the connector.
    result = CliRunner().invoke(shares, ["create", "host-" + "a" * 32, "--workspace-id", "host-" + "b" * 32])
    assert result.exit_code == 2
    assert "invalid workspace id" in result.output


def test_status_help_documents_arguments() -> None:
    result = CliRunner().invoke(shares, ["status", "--help"])
    assert result.exit_code == 0
    assert "HOST_ID" in result.output
    assert "--account" in result.output


def test_share_to_json_passes_the_chrome_origin_through() -> None:
    # The desktop reads the chrome origin from this JSON (the CLI enumerates
    # its keys explicitly), so a wire field the model parses but this dict
    # drops would silently strand clients on the connector-origin fallback.
    info = ShareInfo(
        host_id="host-" + "a" * 32,
        workspace_domain="host-" + "a" * 32 + ".b.us1.shares.example",
        region="us1",
        state="active",
        chrome_origin="https://minds.example.com",
    )
    payload = _share_to_json(info, include_token=False)
    assert payload["chrome_origin"] == "https://minds.example.com"

    info_without_chrome = ShareInfo(
        host_id=info.host_id,
        workspace_domain=info.workspace_domain,
        region=info.region,
        state=info.state,
    )
    assert _share_to_json(info_without_chrome, include_token=False)["chrome_origin"] is None


def test_share_to_json_passes_the_needs_reshare_flag_through() -> None:
    # The desktop decides whether to repair a share from this key.
    stale = ShareInfo(
        host_id="host-" + "a" * 32,
        workspace_domain="host-" + "a" * 32 + ".b.us1.old.example",
        region="us1",
        state="active",
        needs_reshare=True,
    )
    assert _share_to_json(stale, include_token=False)["needs_reshare"] is True

    current = ShareInfo(
        host_id=stale.host_id,
        workspace_domain=stale.workspace_domain,
        region=stale.region,
        state=stale.state,
    )
    assert _share_to_json(current, include_token=False)["needs_reshare"] is False


def test_set_grantees_help_documents_the_repeatable_user_id_option() -> None:
    result = CliRunner().invoke(shares, ["set-grantees", "--help"])
    assert result.exit_code == 0
    assert "HOST_ID" in result.output
    assert "--user-id" in result.output


def test_invite_help_documents_the_two_subjects_and_refuses_both_or_neither() -> None:
    runner = CliRunner()
    help_result = runner.invoke(shares, ["invite", "--help"])
    neither = runner.invoke(shares, ["invite", "host-abc"])
    both = runner.invoke(shares, ["invite", "host-abc", "--email", "bob@example.com", "--user-id", "u-1"])

    assert help_result.exit_code == 0
    for option in ("--user-id", "--email", "--app", "--link", "--workspace-name"):
        assert option in help_result.output
    assert neither.exit_code == 2 and "exactly one" in neither.output
    assert both.exit_code == 2 and "exactly one" in both.output


def test_push_grants_requires_an_existing_document_file() -> None:
    result = CliRunner().invoke(shares, ["push-grants", "host-abc", "--document-file", "/nonexistent/grants.json"])

    assert result.exit_code == 2
    assert "does not exist" in result.output
