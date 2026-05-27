"""Unit tests for framework utility functions."""

from imbue.mngr_mapreduce.utils import dedup_name
from imbue.mngr_mapreduce.utils import make_run_name
from imbue.mngr_mapreduce.utils import sanitize_for_agent_name


def test_make_run_name_format() -> None:
    name = make_run_name()
    assert len(name) == 14
    assert name.isdigit()


def test_dedup_name_first_use_returns_base() -> None:
    used: set[str] = set()
    assert dedup_name("foo", used) == "foo"
    assert used == {"foo"}


def test_dedup_name_collision_appends_counter() -> None:
    used: set[str] = {"foo"}
    assert dedup_name("foo", used) == "foo-2"
    assert dedup_name("foo", used) == "foo-3"
    assert used == {"foo", "foo-2", "foo-3"}


def test_dedup_name_skips_existing_counters() -> None:
    used: set[str] = {"foo", "foo-2"}
    assert dedup_name("foo", used) == "foo-3"


def test_sanitize_simple_name() -> None:
    assert sanitize_for_agent_name("test_bar") == "test-bar"


def test_sanitize_truncates_long_names() -> None:
    result = sanitize_for_agent_name("a" * 100)
    assert len(result) <= 40


def test_sanitize_special_characters() -> None:
    result = sanitize_for_agent_name("test_with spaces_and___underscores")
    assert " " not in result
    assert "--" not in result


def test_sanitize_strips_leading_and_trailing_hyphens() -> None:
    assert sanitize_for_agent_name("__foo__") == "foo"
