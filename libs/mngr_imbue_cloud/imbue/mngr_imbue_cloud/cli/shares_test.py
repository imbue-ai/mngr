from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.shares import _share_to_json
from imbue.mngr_imbue_cloud.cli.shares import shares
from imbue.mngr_imbue_cloud.wire_types import ShareInfo


def test_shares_group_lists_subcommands() -> None:
    result = CliRunner().invoke(shares, ["--help"])
    assert result.exit_code == 0
    for name in ("create", "delete", "status", "list", "relays"):
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
