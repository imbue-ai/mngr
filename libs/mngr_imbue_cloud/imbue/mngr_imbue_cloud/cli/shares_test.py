from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.shares import shares


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
