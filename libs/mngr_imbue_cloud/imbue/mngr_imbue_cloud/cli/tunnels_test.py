from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.tunnels import tunnels


def test_tunnels_group_lists_subcommands() -> None:
    result = CliRunner().invoke(tunnels, ["--help"])
    assert result.exit_code == 0
    for name in ("create", "list", "find-by-agent", "enable-sharing", "delete", "services", "auth"):
        assert name in result.output


def test_find_by_agent_help_documents_agent_argument() -> None:
    result = CliRunner().invoke(tunnels, ["find-by-agent", "--help"])
    assert result.exit_code == 0
    assert "AGENT_ID" in result.output


def test_enable_sharing_help_documents_arguments() -> None:
    result = CliRunner().invoke(tunnels, ["enable-sharing", "--help"])
    assert result.exit_code == 0
    assert "AGENT_ID" in result.output
    assert "SERVICE_NAME" in result.output
    assert "--policy" in result.output
