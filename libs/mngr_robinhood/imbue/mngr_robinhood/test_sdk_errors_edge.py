"""Edge-case behavior of the documented session functions (no API calls).

These complement ``test_sdk_errors.py``: they pin the documented return/raise behavior of the
session readers for unknown sessions and paging arguments. Grouped into the opt-in live suite
for cohesion, but they make no API calls.
"""

from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.timeout(60)]

_UNKNOWN_SESSION_ID = "11111111-2222-3333-4444-555555555555"


def test_get_session_messages_unknown_returns_empty_list(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.get_session_messages(_UNKNOWN_SESSION_ID, directory=str(sdk_cwd)) == []


def test_get_session_messages_unknown_with_limit_returns_empty(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.get_session_messages(_UNKNOWN_SESSION_ID, directory=str(sdk_cwd), limit=5) == []


def test_get_session_messages_unknown_with_offset_returns_empty(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.get_session_messages(_UNKNOWN_SESSION_ID, directory=str(sdk_cwd), offset=10) == []


def test_tag_clear_on_unknown_session_raises_file_not_found(sdk: ModuleType, sdk_cwd: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sdk.tag_session(_UNKNOWN_SESSION_ID, None, directory=str(sdk_cwd))


def test_list_sessions_with_limit_on_empty_directory_returns_empty(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.list_sessions(directory=str(sdk_cwd), limit=10) == []


def test_list_sessions_excluding_worktrees_on_empty_directory_returns_empty(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.list_sessions(directory=str(sdk_cwd), include_worktrees=False) == []
