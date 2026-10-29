from pathlib import Path

import pytest

from imbue.minds.desktop_client.minds_config import MAX_RECENT_ACCOUNTS
from imbue.minds.desktop_client.minds_config import MAX_RECENT_WORKSPACES_PER_ACCOUNT
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.testing import ReadCountingMindsConfig
from imbue.minds.desktop_client.testing import WriteCountingMindsConfig
from imbue.minds.errors import MindsConfigError


def _make_config(tmp_path: Path) -> MindsConfig:
    return MindsConfig(data_dir=tmp_path)


def test_default_values_when_no_file(tmp_path: Path) -> None:
    """Default values are returned when config.toml does not exist."""
    config = _make_config(tmp_path)
    assert config.get_recent_account_ids() == []
    assert config.get_report_unexpected_errors() is True


def test_onboarding_complete_defaults_false_and_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_is_onboarding_complete() is False
    config.set_is_onboarding_complete(True)
    assert config.get_is_onboarding_complete() is True
    # Persisted to the file, not held in memory: a second store reads it back.
    assert _make_config(tmp_path).get_is_onboarding_complete() is True


def test_browser_import_offered_defaults_false_and_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_is_browser_import_offered() is False
    config.set_is_browser_import_offered(True)
    assert config.get_is_browser_import_offered() is True
    assert _make_config(tmp_path).get_is_browser_import_offered() is True


def test_sign_in_browser_id_round_trips_and_clears(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_sign_in_browser_id() is None

    config.set_sign_in_browser_id("/Applications/Firefox.app")
    assert _make_config(tmp_path).get_sign_in_browser_id() == "/Applications/Firefox.app"

    config.set_sign_in_browser_id(None)
    assert _make_config(tmp_path).get_sign_in_browser_id() is None


def test_note_window_shows_moves_the_account_and_its_workspace_to_the_front(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.note_window_shows("user-a", "agent-1") is True
    assert config.note_window_shows("user-b", None) is True
    assert config.note_window_shows("user-a", "agent-2") is True

    assert config.get_recent_account_ids() == ["user-a", "user-b"]
    assert config.get_recent_workspace_ids("user-a") == ["agent-2", "agent-1"]
    assert config.get_recent_workspace_ids("user-b") == []
    # Persisted to the file, not held in memory: a second store reads it back.
    assert _make_config(tmp_path).get_recent_workspace_ids("user-a") == ["agent-2", "agent-1"]


def test_note_window_shows_writes_nothing_when_the_order_is_unchanged(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    config.note_window_shows("user-a", "agent-1")
    assert config.write_count == 1

    assert config.note_window_shows("user-a", "agent-1") is False
    assert config.note_window_shows("user-a", None) is False
    assert config.write_count == 1
    assert config.get_recent_workspace_ids("user-a") == ["agent-1"]


def test_recent_lists_are_bounded(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    for index in range(MAX_RECENT_ACCOUNTS + 5):
        config.note_window_shows(f"user-{index}", None)
    for index in range(MAX_RECENT_WORKSPACES_PER_ACCOUNT + 5):
        config.note_window_shows("user-0", f"agent-{index}")

    # Trimming drops the oldest entries: the workspace reports put user-0 back at
    # the front, and the newest accounts follow it down to the trimmed tail.
    newest_accounts = [f"user-{index}" for index in range(MAX_RECENT_ACCOUNTS + 4, 0, -1)]
    assert config.get_recent_account_ids() == ["user-0"] + newest_accounts[: MAX_RECENT_ACCOUNTS - 1]
    assert config.get_recent_workspace_ids("user-0") == [
        f"agent-{index}" for index in range(MAX_RECENT_WORKSPACES_PER_ACCOUNT + 4, 4, -1)
    ]


def test_default_region_is_none(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_region("imbue_cloud") is None


def test_set_and_get_region_per_provider(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_region("imbue_cloud", "US-WEST-OR")
    config.set_region("vultr", "lhr")
    assert config.get_region("imbue_cloud") == "US-WEST-OR"
    assert config.get_region("vultr") == "lhr"
    # A provider with no stored region still reads as None.
    assert config.get_region("docker") is None


def test_set_region_overwrites_previous_value(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_region("imbue_cloud", "US-EAST-VA")
    config.set_region("imbue_cloud", "US-WEST-OR")
    assert config.get_region("imbue_cloud") == "US-WEST-OR"


def test_error_reporting_defaults(tmp_path: Path) -> None:
    """On a fresh install the consent notice is unanswered, but reporting defaults ON for new users."""
    config = _make_config(tmp_path)
    assert config.get_error_reporting_consent_given() is False
    assert config.get_report_unexpected_errors() is True


def test_error_reporting_settings_round_trip(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_error_reporting_consent_given(True)
    config.set_report_unexpected_errors(False)
    assert config.get_error_reporting_consent_given() is True
    assert config.get_report_unexpected_errors() is False
    # A new instance reads the same persisted values.
    reloaded = _make_config(tmp_path)
    assert reloaded.get_error_reporting_consent_given() is True
    assert reloaded.get_report_unexpected_errors() is False


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Config written by one instance is readable by a new instance."""
    config1 = _make_config(tmp_path)
    config1.set_sign_in_browser_id("/Applications/Firefox.app")
    config1.set_report_unexpected_errors(False)

    config2 = _make_config(tmp_path)
    assert config2.get_sign_in_browser_id() == "/Applications/Firefox.app"
    assert config2.get_report_unexpected_errors() is False


def test_a_failed_write_raises_a_config_error(tmp_path: Path) -> None:
    """A write that the filesystem refuses is reported as the config's own error, like a failed read."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    config = MindsConfig(data_dir=blocker)
    with pytest.raises(MindsConfigError):
        config.note_window_shows("user-a", None)


def test_corrupt_toml_raises(tmp_path: Path) -> None:
    """A corrupt config.toml raises MindsConfigError rather than silently
    returning defaults. Silent fallback would hide data corruption -- e.g.
    the next ``set_*`` call would overwrite the unparseable file with a
    fresh one derived from an empty dict, losing whatever the user had
    intended to be stored.
    """
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("not valid toml {{{")
    with pytest.raises(MindsConfigError):
        config.get_recent_account_ids()
    with pytest.raises(MindsConfigError):
        config.get_report_unexpected_errors()


def test_multiple_settings_coexist(tmp_path: Path) -> None:
    """Setting one value does not clobber other values."""
    config = _make_config(tmp_path)
    config.note_window_shows("user-xyz", None)
    config.set_report_unexpected_errors(False)

    assert config.get_recent_account_ids() == ["user-xyz"]
    assert config.get_report_unexpected_errors() is False

    config.note_window_shows("user-new", None)
    assert config.get_report_unexpected_errors() is False


def test_notification_style_defaults_with_no_choice(tmp_path: Path) -> None:
    """On a fresh install: style 'both', nothing chosen yet."""
    config = _make_config(tmp_path)
    assert config.get_notification_style_with_choice() == (NotificationStyle.BOTH, False)


def test_malformed_notification_style_falls_back_to_the_default(tmp_path: Path) -> None:
    """A malformed stored style reads as the default rather than exploding, mirroring _get_bool."""
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text('notification_style = "shout"\n')
    assert config.get_notification_style_with_choice()[0] == "both"
    (tmp_path / "config.toml").write_text("notification_style = 3\n")
    assert config.get_notification_style_with_choice()[0] == "both"


def test_an_existing_saved_style_counts_as_a_choice(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('notification_style = "cards"\n')
    assert _make_config(tmp_path).get_notification_style_with_choice() == (NotificationStyle.CARDS, True)


def test_a_legacy_master_toggle_in_the_config_still_counts_as_a_choice(tmp_path: Path) -> None:
    """An install the migration has not visited may still hold the moved key; its presence is a saved choice."""
    (tmp_path / "config.toml").write_text("notifications_enabled = false\n")
    assert _make_config(tmp_path).get_notification_style_with_choice() == (NotificationStyle.BOTH, True)


def test_get_notification_style_with_choice_reads_under_one_lock_acquisition(tmp_path: Path) -> None:
    """One _read_raw() call, not two: the style and the choice flag feed one If-Match version,
    so they must come from one snapshot of the file."""
    config = ReadCountingMindsConfig(data_dir=tmp_path)
    read_count_before = config.read_count

    config.get_notification_style_with_choice()

    assert config.read_count - read_count_before == 1


def _notification_style_test_version(style: NotificationStyle, has_chosen: bool = False) -> str:
    """A trivial, deterministic version stamp -- these tests only care that
    set_notification_style_if_version_matches treats a mismatch as a mismatch, not about
    the real hashing scheme (that lives in ui_api_settings.py)."""
    return f"{style}:{has_chosen}"


def test_set_notification_style_if_version_matches_applies_on_a_matching_version(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    starting_version = _notification_style_test_version(NotificationStyle.BOTH)

    new_version = config.set_notification_style_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_style_test_version,
        style=NotificationStyle.OS,
        is_chosen_elsewhere=False,
    )

    assert new_version == _notification_style_test_version(NotificationStyle.OS, True)
    assert config.write_count == 1
    assert config.get_notification_style_with_choice() == (NotificationStyle.OS, True)


def test_set_notification_style_if_version_matches_rejects_a_stale_version_without_writing(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    stale_version = _notification_style_test_version(NotificationStyle.BOTH)
    # A first writer applied its change...
    (tmp_path / "config.toml").write_text('notification_style = "os"\n')

    # ...so a second writer that started from the now-stale version must be rejected, not
    # silently clobber the first writer's change.
    result = config.set_notification_style_if_version_matches(
        expected_version=stale_version,
        compute_version=_notification_style_test_version,
        style=NotificationStyle.CARDS,
        is_chosen_elsewhere=False,
    )

    assert result is None
    assert config.write_count == 0
    assert config.get_notification_style_with_choice() == (NotificationStyle.OS, True)


def test_set_notification_style_if_version_matches_closes_the_check_then_act_race(tmp_path: Path) -> None:
    """Two 'requests' racing from the same starting version: calling the atomic
    compare-and-swap twice in a row with the SAME expected_version (rather than a separate
    locked get and set, which would let both pass their version check before either
    applies) must reject the second as stale instead of letting it silently clobber the
    first."""
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    starting_version = _notification_style_test_version(NotificationStyle.BOTH)

    first = config.set_notification_style_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_style_test_version,
        style=NotificationStyle.OS,
        is_chosen_elsewhere=False,
    )
    second = config.set_notification_style_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_style_test_version,
        style=NotificationStyle.CARDS,
        is_chosen_elsewhere=False,
    )

    assert first is not None
    assert second is None
    assert config.write_count == 1
    assert config.get_notification_style_with_choice()[0] == NotificationStyle.OS


def test_the_style_cas_folds_in_a_choice_recorded_in_the_settings_records(tmp_path: Path) -> None:
    """The served version ORs in a master-toggle choice from the account settings records,
    so the check here must see the same has_chosen or every write would 412."""
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    version_with_choice = _notification_style_test_version(NotificationStyle.BOTH, True)

    new_version = config.set_notification_style_if_version_matches(
        expected_version=version_with_choice,
        compute_version=_notification_style_test_version,
        style=NotificationStyle.CARDS,
        is_chosen_elsewhere=True,
    )

    assert new_version == _notification_style_test_version(NotificationStyle.CARDS, True)


def test_a_style_write_retires_the_stored_os_hint_flag(tmp_path: Path) -> None:
    """A config written by a build with the browser-mode hint loses that key on the next style write."""
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("notification_os_hint_dismissed = true\n")

    config.set_notification_style_if_version_matches(
        expected_version=_notification_style_test_version(NotificationStyle.BOTH, True),
        compute_version=_notification_style_test_version,
        style=NotificationStyle.OS,
        # The hint key predates the choice flag's config half, so the stored
        # record reads as already chosen through the legacy toggle check.
        is_chosen_elsewhere=True,
    )

    assert "notification_os_hint_dismissed" not in (tmp_path / "config.toml").read_text()
    assert config.get_notification_style_with_choice()[0] == NotificationStyle.OS


def test_set_report_unexpected_errors_if_version_matches_applies_on_a_matching_version(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)

    new_version = config.set_report_unexpected_errors_if_version_matches(
        expected_version="True", compute_version=str, enabled=False
    )

    assert new_version == "False"
    assert config.write_count == 1
    assert config.get_report_unexpected_errors() is False


def test_set_report_unexpected_errors_if_version_matches_closes_the_check_then_act_race(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)

    first = config.set_report_unexpected_errors_if_version_matches(
        expected_version="True", compute_version=str, enabled=False
    )
    second = config.set_report_unexpected_errors_if_version_matches(
        expected_version="True", compute_version=str, enabled=True
    )

    assert first == "False"
    assert second is None
    assert config.write_count == 1
    assert config.get_report_unexpected_errors() is False


def test_migration_reads_every_present_account_keyed_value(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text(
        "notifications_enabled = false\n"
        "[updates]\n"
        "window_start_hour = 23\n"
        "window_end_hour = 3\n"
        "auto_update_workspaces = true\n"
        'auto_update_prompt_decision = "accepted"\n'
    )

    assert config.read_account_keyed_settings_for_migration() == {
        "update_window_start_hour": 23,
        "update_window_end_hour": 3,
        "auto_update_workspaces": True,
        "auto_update_prompt_decision": "accepted",
        "notifications_enabled": False,
    }


def test_migration_reads_nothing_from_a_fresh_or_already_migrated_config(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.read_account_keyed_settings_for_migration() == {}
    config.set_sign_in_browser_id("someone")
    assert config.read_account_keyed_settings_for_migration() == {}


def _toml_literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


@pytest.mark.parametrize(
    "stored",
    (
        {"window_start_hour": 2},
        {"window_start_hour": "2", "window_end_hour": "5"},
        {"window_start_hour": True, "window_end_hour": 5},
        {"window_start_hour": 2, "window_end_hour": 24},
        {"window_start_hour": -1, "window_end_hour": 5},
        {"window_start_hour": 3, "window_end_hour": 3},
    ),
    ids=["half-written", "wrong-type", "bool-hour", "hour-too-high", "hour-negative", "empty-window"],
)
def test_migration_skips_an_unusable_stored_update_window(tmp_path: Path, stored: dict[str, object]) -> None:
    """An unusable pair never seeds the records: the carrier's defaults answer instead, as the old getter did."""
    config = _make_config(tmp_path)
    lines = [f"{key} = {_toml_literal(value)}" for key, value in stored.items()]
    (tmp_path / "config.toml").write_text("[updates]\n" + "\n".join(lines) + "\n")

    assert config.read_account_keyed_settings_for_migration() == {}


def test_migration_delete_removes_only_the_moved_keys(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text(
        "notifications_enabled = true\n"
        'notification_style = "cards"\n'
        "[updates]\n"
        "window_start_hour = 23\n"
        "window_end_hour = 3\n"
        "auto_update_workspaces = true\n"
        "[updates.skipped_targets]\n"
        '"agent-1" = "minds-v1"\n'
    )

    config.delete_account_keyed_settings_for_migration()

    raw = (tmp_path / "config.toml").read_text()
    assert "notifications_enabled" not in raw
    assert "window_start_hour" not in raw
    assert "auto_update_workspaces" not in raw
    # The style is install-keyed and stays; the skipped targets move in their own (3B) migration.
    assert config.get_notification_style_with_choice() == (NotificationStyle.CARDS, True)
    assert config.read_skipped_update_targets_for_migration() == {"agent-1": "minds-v1"}


def test_migration_delete_drops_an_emptied_updates_table(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("[updates]\nauto_update_workspaces = true\n")

    config.delete_account_keyed_settings_for_migration()

    assert "updates" not in (tmp_path / "config.toml").read_text()
