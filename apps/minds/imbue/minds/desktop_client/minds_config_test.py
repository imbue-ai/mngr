from pathlib import Path

import pytest

from imbue.minds.desktop_client.minds_config import DEFAULT_UPDATE_WINDOW
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
    assert config.get_default_account_id() is None
    assert config.get_report_unexpected_errors() is True


def test_set_and_get_default_account_id(tmp_path: Path) -> None:
    """Setting and getting default_account_id works correctly."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    assert config.get_default_account_id() == "user-123"


def test_clear_default_account_id(tmp_path: Path) -> None:
    """Clearing the default account sets it to None."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    config.set_default_account_id(None)
    assert config.get_default_account_id() is None


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
    config1.set_default_account_id("user-abc")
    config1.set_report_unexpected_errors(False)

    config2 = _make_config(tmp_path)
    assert config2.get_default_account_id() == "user-abc"
    assert config2.get_report_unexpected_errors() is False


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
        config.get_default_account_id()
    with pytest.raises(MindsConfigError):
        config.get_report_unexpected_errors()


def test_multiple_settings_coexist(tmp_path: Path) -> None:
    """Setting one value does not clobber other values."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-xyz")
    config.set_report_unexpected_errors(False)

    assert config.get_default_account_id() == "user-xyz"
    assert config.get_report_unexpected_errors() is False

    config.set_default_account_id("user-new")
    assert config.get_report_unexpected_errors() is False


def test_notification_prefs_defaults(tmp_path: Path) -> None:
    """On a fresh install: nudges on, style 'both', hint not dismissed."""
    config = _make_config(tmp_path)
    is_enabled, style, is_os_hint_dismissed = config.get_notification_prefs()
    assert is_enabled is True
    assert style == "both"
    assert is_os_hint_dismissed is False


def test_notification_prefs_round_trip(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.OS, is_os_hint_dismissed=True)
    assert config.get_notification_prefs() == (False, NotificationStyle.OS, True)
    # A new instance reads the same persisted values.
    reloaded = _make_config(tmp_path)
    assert reloaded.get_notification_prefs() == (False, NotificationStyle.OS, True)


def test_set_notification_prefs_persists_all_three_keys_in_one_write(tmp_path: Path) -> None:
    """The combined setter is one read-modify-write, so a concurrent writer can
    never observe (or interleave with) a half-updated record."""
    config = WriteCountingMindsConfig(data_dir=tmp_path)

    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.OS, is_os_hint_dismissed=True)

    assert config.write_count == 1
    reloaded = _make_config(tmp_path)
    assert reloaded.get_notification_prefs() == (False, NotificationStyle.OS, True)


def test_set_notification_prefs_replaces_a_full_prior_record_wholesale(tmp_path: Path) -> None:
    """Two full-record writes land as one record or the other, never a mix."""
    config = _make_config(tmp_path)
    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.OS, is_os_hint_dismissed=False)

    config.set_notification_prefs(is_enabled=True, style=NotificationStyle.CARDS, is_os_hint_dismissed=True)

    assert config.get_notification_prefs() == (True, NotificationStyle.CARDS, True)


def test_get_notification_prefs_reads_all_fields_under_one_lock_acquisition(tmp_path: Path) -> None:
    """One _read_raw() call, not three: separate locked reads could observe a concurrent
    set_notification_prefs() writer's update to only some of the fields -- a combination that
    write never actually persisted together."""
    config = ReadCountingMindsConfig(data_dir=tmp_path)
    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.OS, is_os_hint_dismissed=True)
    read_count_before = config.read_count

    config.get_notification_prefs()

    assert config.read_count - read_count_before == 1


def test_get_notification_prefs_round_trips_with_set_notification_prefs(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_notification_prefs() == (True, NotificationStyle.BOTH, False)

    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.CARDS, is_os_hint_dismissed=True)

    assert config.get_notification_prefs() == (False, NotificationStyle.CARDS, True)


def test_malformed_notification_style_falls_back_to_the_default(tmp_path: Path) -> None:
    """A malformed stored style reads as the default rather than exploding, mirroring _get_bool."""
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text('notification_style = "shout"\n')
    assert config.get_notification_prefs()[1] == "both"
    (tmp_path / "config.toml").write_text("notification_style = 3\n")
    assert config.get_notification_prefs()[1] == "both"


def _notification_prefs_test_version(is_enabled: bool, style: NotificationStyle, is_os_hint_dismissed: bool) -> str:
    """A trivial, deterministic version stamp -- these tests only care that
    set_notification_prefs_if_version_matches treats a mismatch as a mismatch, not about
    the real hashing scheme (that lives in ui_api_settings.py)."""
    return f"{is_enabled}:{style}:{is_os_hint_dismissed}"


def test_set_notification_prefs_if_version_matches_applies_on_a_matching_version(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    starting_version = _notification_prefs_test_version(True, NotificationStyle.BOTH, False)

    new_version = config.set_notification_prefs_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_prefs_test_version,
        is_enabled=False,
        style=NotificationStyle.OS,
        is_os_hint_dismissed=True,
    )

    assert new_version == _notification_prefs_test_version(False, NotificationStyle.OS, True)
    assert config.write_count == 1
    assert config.get_notification_prefs() == (False, NotificationStyle.OS, True)


def test_set_notification_prefs_if_version_matches_rejects_a_stale_version_without_writing(tmp_path: Path) -> None:
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    stale_version = _notification_prefs_test_version(True, NotificationStyle.BOTH, False)
    # A first writer applies its change...
    config.set_notification_prefs(is_enabled=False, style=NotificationStyle.OS, is_os_hint_dismissed=True)

    # ...so a second writer that started from the now-stale version must be rejected, not
    # silently clobber the first writer's change.
    result = config.set_notification_prefs_if_version_matches(
        expected_version=stale_version,
        compute_version=_notification_prefs_test_version,
        is_enabled=True,
        style=NotificationStyle.CARDS,
        is_os_hint_dismissed=False,
    )

    assert result is None
    # Only the first writer's write landed.
    assert config.write_count == 1
    assert config.get_notification_prefs() == (False, NotificationStyle.OS, True)


def test_set_notification_prefs_if_version_matches_closes_the_check_then_act_race(tmp_path: Path) -> None:
    """Two 'requests' racing from the same starting version: calling the atomic
    compare-and-swap twice in a row with the SAME expected_version (rather than calling
    get_notification_prefs() and set_notification_prefs() separately, which would let both
    pass their version check before either applies) must reject the second as stale instead
    of letting it silently clobber the first."""
    config = WriteCountingMindsConfig(data_dir=tmp_path)
    starting_version = _notification_prefs_test_version(True, NotificationStyle.BOTH, False)

    first = config.set_notification_prefs_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_prefs_test_version,
        is_enabled=False,
        style=NotificationStyle.OS,
        is_os_hint_dismissed=False,
    )
    second = config.set_notification_prefs_if_version_matches(
        expected_version=starting_version,
        compute_version=_notification_prefs_test_version,
        is_enabled=True,
        style=NotificationStyle.CARDS,
        is_os_hint_dismissed=True,
    )

    assert first is not None
    assert second is None
    assert config.write_count == 1
    assert config.get_notification_prefs() == (False, NotificationStyle.OS, False)


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


def test_update_window_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    assert config.get_update_window() == DEFAULT_UPDATE_WINDOW

    config.set_update_window(23, 3)

    assert config.get_update_window() == (23, 3)
    assert _make_config(tmp_path).get_update_window() == (23, 3)


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
def test_an_unusable_stored_update_window_falls_back_rather_than_raising(
    tmp_path: Path, stored: dict[str, object]
) -> None:
    config = _make_config(tmp_path)
    config.set_default_account_id("someone")
    raw = (tmp_path / "config.toml").read_text()
    lines = [f"{key} = {_toml_literal(value)}" for key, value in stored.items()]
    (tmp_path / "config.toml").write_text(raw + "\n[updates]\n" + "\n".join(lines) + "\n")

    assert config.get_update_window() == DEFAULT_UPDATE_WINDOW
