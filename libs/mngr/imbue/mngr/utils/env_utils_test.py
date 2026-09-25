"""Unit tests for environment utilities."""

from datetime import datetime
from datetime import timezone
from uuid import uuid4

import pytest

from imbue.mngr.utils.env_utils import InvalidTestUserIdBudgetError
from imbue.mngr.utils.env_utils import TEST_ENV_PATTERN
from imbue.mngr.utils.env_utils import TEST_ENV_PREFIX
from imbue.mngr.utils.env_utils import build_test_user_id
from imbue.mngr.utils.env_utils import looks_like_mngr_test_container_name
from imbue.mngr.utils.env_utils import parse_bool_env
from imbue.mngr.utils.env_utils import parse_env_file


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes", "YES"])
def test_parse_bool_env_truthy(value: str) -> None:
    assert parse_bool_env(value) is True


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "No", "anything", "2"])
def test_parse_bool_env_falsy(value: str) -> None:
    assert parse_bool_env(value) is False


def test_parse_env_file_simple() -> None:
    """Test parsing simple env file."""
    content = "FOO=bar\nBAZ=qux"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_comments() -> None:
    """Test parsing env file with comments."""
    content = "# comment\nFOO=bar\n# another comment\nBAZ=qux"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_quotes() -> None:
    """Test parsing env file with quoted values."""
    content = "FOO=\"bar baz\"\nBAR='qux'"
    env = parse_env_file(content)
    assert env == {"FOO": "bar baz", "BAR": "qux"}


def test_parse_env_file_empty_lines() -> None:
    """Test parsing env file with empty lines."""
    content = "FOO=bar\n\nBAZ=qux\n"
    env = parse_env_file(content)
    assert env == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_with_mixed_quote_styles() -> None:
    """Test parsing env file with mixed quote styles."""
    content = "A=\"val1\"\nB='val2'\nC=val3"
    env = parse_env_file(content)
    assert env == {"A": "val1", "B": "val2", "C": "val3"}


def test_parse_env_file_with_spaces_in_unquoted_value() -> None:
    """Test parsing env file with spaces in unquoted value."""
    content = "KEY=value with spaces"
    env = parse_env_file(content)
    assert env["KEY"] == "value with spaces"


def test_parse_env_file_with_multiple_equals_unquoted() -> None:
    """Test parsing env file with multiple equals signs in value."""
    content = "KEY=a=b=c"
    env = parse_env_file(content)
    assert env["KEY"] == "a=b=c"


def test_looks_like_mngr_test_container_name_matches_real_per_test_prefix() -> None:
    # A real per-test prefix is "mngr_<uuid4().hex>-"; the state container name
    # appends "docker-state-<user_id>". This is the exact shape the off-by-one
    # in the old detector failed to match.
    name = f"mngr_{uuid4().hex}-docker-state-{uuid4().hex}"
    assert looks_like_mngr_test_container_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        # Real production singletons (no per-test prefix) must NOT match, so the
        # safety net never sweeps them.
        "mngr-docker-state-715245b5075646fb8b55ca949a291049",
        "minds-docker-state-034db0c4426e4ef187711c49fd0310ca",
        # Wrong hex length (31 chars), not a uuid4().hex prefix.
        "mngr_0123456789abcdef0123456789abcde-docker-state-x",
        # Non-hex characters in the prefix.
        "mngr_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ-docker-state-x",
        # Missing the trailing dash after the hex.
        "mngr_22921e597952421296c8973d922f2eb3",
        "",
    ],
)
def test_looks_like_mngr_test_container_name_rejects_non_test_names(name: str) -> None:
    assert looks_like_mngr_test_container_name(name) is False


_TIMESTAMP = datetime(2026, 9, 21, 8, 20, 42, tzinfo=timezone.utc)

# The shape of a TMR mapper's agent name: a run prefix and a test slug, 64 characters in all.
_LONG_AGENT_NAME = "tmr-mngr-20260921082042-test-create-modal-custom-dockerfile-only"

# What is left for the user id once the Modal 64-character name limit has paid for the mngr_test- prefix.
_MODAL_USER_ID_BUDGET = 64 - len(TEST_ENV_PREFIX)


def test_build_test_user_id_fits_a_long_agent_name_into_the_budget_and_keeps_the_suffix_last() -> None:
    user_id = build_test_user_id(_TIMESTAMP, _LONG_AGENT_NAME, "0badf00d", _MODAL_USER_ID_BUDGET)

    assert len(user_id) == _MODAL_USER_ID_BUDGET
    assert user_id.startswith("2026-09-21-08-20-42-tmr-mngr-20260921082042")
    assert user_id.endswith("-0badf00d")
    assert TEST_ENV_PATTERN.match(TEST_ENV_PREFIX + user_id) is not None


def test_build_test_user_id_keeps_the_suffix_distinct_when_two_sessions_share_a_name_and_a_second() -> None:
    """The 2026-09-21 TMR run: mappers whose slugs share their first characters started in the same second."""
    first = build_test_user_id(_TIMESTAMP, _LONG_AGENT_NAME, "11111111", _MODAL_USER_ID_BUDGET)
    second = build_test_user_id(_TIMESTAMP, _LONG_AGENT_NAME, "22222222", _MODAL_USER_ID_BUDGET)

    assert first != second
    assert first[:-8] == second[:-8]


def test_build_test_user_id_omits_the_name_part_without_an_agent_name() -> None:
    assert build_test_user_id(_TIMESTAMP, None, "cafe1234", _MODAL_USER_ID_BUDGET) == "2026-09-21-08-20-42-cafe1234"


def test_build_test_user_id_keeps_a_short_agent_name_whole() -> None:
    assert build_test_user_id(_TIMESTAMP, "my-agent", "cafe1234", _MODAL_USER_ID_BUDGET) == (
        "2026-09-21-08-20-42-my-agent-cafe1234"
    )


def test_build_test_user_id_does_not_end_the_name_part_with_a_dash_after_cutting_it() -> None:
    user_id = build_test_user_id(_TIMESTAMP, "abc-def", "cafe1234", len("2026-09-21-08-20-42-cafe1234") + 5)

    assert user_id == "2026-09-21-08-20-42-abc-cafe1234"


def test_build_test_user_id_rejects_a_budget_too_small_for_the_timestamp_and_suffix() -> None:
    with pytest.raises(InvalidTestUserIdBudgetError):
        build_test_user_id(_TIMESTAMP, None, "cafe1234", len("2026-09-21-08-20-42-cafe1234") - 1)
