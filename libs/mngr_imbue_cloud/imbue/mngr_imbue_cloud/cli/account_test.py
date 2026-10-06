from click.testing import CliRunner

from imbue.mngr_imbue_cloud.cli.account import account


def test_account_group_lists_the_notification_preferences_commands() -> None:
    listing = CliRunner().invoke(account, ["notification-preferences", "--help"])
    set_help = CliRunner().invoke(account, ["notification-preferences", "set", "--help"])

    assert listing.exit_code == 0
    assert "show" in listing.output and "set" in listing.output
    assert set_help.exit_code == 0
    assert "--email / --no-email" in set_help.output
    assert "--in-app / --no-in-app" in set_help.output


def test_set_notification_preferences_requires_both_switches() -> None:
    result = CliRunner().invoke(account, ["notification-preferences", "set", "--email"])

    assert result.exit_code == 2
    assert "--in-app" in result.output
