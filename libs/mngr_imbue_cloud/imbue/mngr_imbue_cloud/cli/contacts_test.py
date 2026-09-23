from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.contacts import contacts


def test_contacts_group_lists_subcommands() -> None:
    result = CliRunner().invoke(contacts, ["--help"])
    assert result.exit_code == 0
    for name in ("list", "add", "remove"):
        assert name in result.output


def test_add_help_documents_the_argument() -> None:
    result = CliRunner().invoke(contacts, ["add", "--help"])
    assert result.exit_code == 0
    assert "USER_ID" in result.output
    assert "--account" in result.output
