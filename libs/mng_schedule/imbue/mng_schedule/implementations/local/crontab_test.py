"""Unit tests for crontab.py pure functions."""

from imbue.mng_schedule.implementations.local.crontab import add_crontab_entry
from imbue.mng_schedule.implementations.local.crontab import build_marker_comment
from imbue.mng_schedule.implementations.local.crontab import list_managed_trigger_names
from imbue.mng_schedule.implementations.local.crontab import remove_crontab_entry

_TEST_PREFIX = "mng-test-"


def test_build_marker_comment() -> None:
    assert build_marker_comment("mng-", "nightly") == "# mng-schedule:nightly"


def test_build_marker_comment_with_test_prefix() -> None:
    assert build_marker_comment("mng-test-", "nightly") == "# mng-test-schedule:nightly"


def test_add_crontab_entry_to_empty_crontab() -> None:
    result = add_crontab_entry(
        existing_content="",
        prefix=_TEST_PREFIX,
        trigger_name="nightly",
        cron_expression="0 2 * * *",
        command="/home/user/.mng/schedule/nightly/run.sh",
    )
    assert f"# {_TEST_PREFIX}schedule:nightly" in result
    assert "0 2 * * * /home/user/.mng/schedule/nightly/run.sh" in result


def test_add_crontab_entry_preserves_existing_entries() -> None:
    existing = "0 1 * * * /some/other/cron/job\n"
    result = add_crontab_entry(
        existing_content=existing,
        prefix=_TEST_PREFIX,
        trigger_name="nightly",
        cron_expression="0 2 * * *",
        command="/home/user/.mng/schedule/nightly/run.sh",
    )
    assert "/some/other/cron/job" in result
    assert f"# {_TEST_PREFIX}schedule:nightly" in result


def test_add_crontab_entry_replaces_existing_trigger() -> None:
    existing = f"# {_TEST_PREFIX}schedule:nightly\n0 2 * * * /old/path/run.sh\n"
    result = add_crontab_entry(
        existing_content=existing,
        prefix=_TEST_PREFIX,
        trigger_name="nightly",
        cron_expression="0 3 * * *",
        command="/new/path/run.sh",
    )
    assert "0 3 * * * /new/path/run.sh" in result
    assert "/old/path/run.sh" not in result
    assert result.count(f"{_TEST_PREFIX}schedule:nightly") == 1


def test_add_crontab_entry_preserves_other_mng_entries() -> None:
    existing = (
        f"# {_TEST_PREFIX}schedule:daily\n"
        "0 1 * * * /daily/run.sh\n"
        f"# {_TEST_PREFIX}schedule:nightly\n"
        "0 2 * * * /nightly/run.sh\n"
    )
    result = add_crontab_entry(
        existing_content=existing,
        prefix=_TEST_PREFIX,
        trigger_name="nightly",
        cron_expression="0 3 * * *",
        command="/new-nightly/run.sh",
    )
    assert "/daily/run.sh" in result
    assert "0 3 * * * /new-nightly/run.sh" in result
    assert "/nightly/run.sh" not in result


def test_remove_crontab_entry_removes_marker_and_cron_line() -> None:
    existing = f"# {_TEST_PREFIX}schedule:nightly\n0 2 * * * /path/run.sh\n0 1 * * * /some/other/job\n"
    result = remove_crontab_entry(existing, _TEST_PREFIX, "nightly")
    assert f"{_TEST_PREFIX}schedule:nightly" not in result
    assert "/path/run.sh" not in result
    assert "/some/other/job" in result


def test_remove_crontab_entry_no_match_returns_unchanged() -> None:
    existing = "0 1 * * * /some/job\n"
    result = remove_crontab_entry(existing, _TEST_PREFIX, "nonexistent")
    assert result == existing


def test_remove_crontab_entry_preserves_other_mng_entries() -> None:
    existing = (
        f"# {_TEST_PREFIX}schedule:daily\n"
        "0 1 * * * /daily/run.sh\n"
        f"# {_TEST_PREFIX}schedule:nightly\n"
        "0 2 * * * /nightly/run.sh\n"
    )
    result = remove_crontab_entry(existing, _TEST_PREFIX, "nightly")
    assert "/daily/run.sh" in result
    assert f"{_TEST_PREFIX}schedule:daily" in result
    assert "/nightly/run.sh" not in result


def test_list_managed_trigger_names_returns_all_names() -> None:
    content = (
        f"# {_TEST_PREFIX}schedule:nightly\n"
        "0 2 * * * /path/run.sh\n"
        f"# {_TEST_PREFIX}schedule:weekly\n"
        "0 3 * * 0 /path/run2.sh\n"
    )
    names = list_managed_trigger_names(content, _TEST_PREFIX)
    assert names == ["nightly", "weekly"]


def test_list_managed_trigger_names_empty_crontab() -> None:
    assert list_managed_trigger_names("", _TEST_PREFIX) == []


def test_list_managed_trigger_names_ignores_non_mng_comments() -> None:
    content = "# some regular comment\n0 1 * * * /job\n"
    assert list_managed_trigger_names(content, _TEST_PREFIX) == []


def test_list_managed_trigger_names_ignores_different_prefix() -> None:
    content = "# other-prefix-schedule:nightly\n0 2 * * * /path/run.sh\n"
    assert list_managed_trigger_names(content, _TEST_PREFIX) == []


def test_add_crontab_entry_appends_newline_to_existing_content_without_trailing_newline() -> None:
    """When existing content doesn't end with a newline, one should be added."""
    existing = "0 1 * * * /some/other/cron/job"
    result = add_crontab_entry(
        existing_content=existing,
        prefix=_TEST_PREFIX,
        trigger_name="nightly",
        cron_expression="0 2 * * *",
        command="/home/user/.mng/schedule/nightly/run.sh",
    )
    assert "/some/other/cron/job" in result
    assert f"# {_TEST_PREFIX}schedule:nightly" in result
    lines = result.splitlines()
    assert lines[0] == "0 1 * * * /some/other/cron/job"
    assert lines[1] == f"# {_TEST_PREFIX}schedule:nightly"
