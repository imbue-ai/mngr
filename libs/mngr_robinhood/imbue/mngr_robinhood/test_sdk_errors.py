"""Verification of the documented error behavior of the session functions.

The docs' "Error Types" section lists ``ValueError`` / ``FileNotFoundError`` / ``AttributeError``.
These tests drive the documented ``FileNotFoundError`` path and the documented
``SDKSessionInfo | None`` return contract through the public session functions. They make no API
calls, but are grouped into the opt-in live suite for cohesion.
"""

from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.sdk_live, pytest.mark.tmux, pytest.mark.timeout(60)]

# A syntactically valid but nonexistent session id.
_UNKNOWN_SESSION_ID = "00000000-0000-0000-0000-000000000000"


def test_list_sessions_in_empty_directory_returns_empty(sdk: ModuleType, sdk_cwd: Path) -> None:
    assert sdk.list_sessions(directory=str(sdk_cwd)) == []


def test_get_session_info_unknown_returns_none(sdk: ModuleType, sdk_cwd: Path) -> None:
    # Documented return type is ``SDKSessionInfo | None``; an unknown id must yield None.
    assert sdk.get_session_info(_UNKNOWN_SESSION_ID, directory=str(sdk_cwd)) is None


def test_rename_unknown_session_raises_file_not_found(sdk: ModuleType, sdk_cwd: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sdk.rename_session(_UNKNOWN_SESSION_ID, "new title", directory=str(sdk_cwd))


def test_tag_unknown_session_raises_file_not_found(sdk: ModuleType, sdk_cwd: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sdk.tag_session(_UNKNOWN_SESSION_ID, "new-tag", directory=str(sdk_cwd))
