"""What the update dispatch actually asks the workspace to do."""

import pytest

from imbue.minds.desktop_client.update_chat import build_update_chat_message


@pytest.mark.witnesses("workspace-updates.one-seed-message", partial="the seed prompt only")
def test_the_dispatch_sends_the_bare_slash_command() -> None:
    """One seed message however the run was started: the skill's own contract already covers an unwatched run."""
    assert build_update_chat_message(target_override=None, is_backup_configured=True) == "/update-self"
    assert build_update_chat_message(target_override="", is_backup_configured=True) == "/update-self"


@pytest.mark.witnesses("workspace-updates.version-override", partial="the seed prompt only")
def test_the_version_override_rides_the_seed_prompt() -> None:
    """The prompt names the target, not the skill's flag or step, which the target version may change."""
    message = build_update_chat_message(target_override="minds-v0.5.0", is_backup_configured=True)

    assert message.startswith("/update-self")
    assert "minds-v0.5.0" in message
    assert "--override" not in message
    assert "resolve-target" not in message


@pytest.mark.witnesses("workspace-updates.no-backup-confirmation", partial="the seed prompt only")
def test_a_run_without_backups_carries_the_users_go_ahead() -> None:
    """The confirmation the app already collected, so an older skill's backup stop does not ask it again."""
    message = build_update_chat_message(target_override=None, is_backup_configured=False)

    assert message.startswith("/update-self")
    assert "no backups configured" in message
    assert "go-ahead" in message


def test_an_overridden_run_without_backups_carries_both_notes() -> None:
    """Neither note displaces the other: the two questions the run must not stop on are independent."""
    message = build_update_chat_message(target_override="minds-v0.5.0", is_backup_configured=False)

    assert message.startswith("/update-self")
    assert "minds-v0.5.0" in message
    assert "no backups configured" in message
