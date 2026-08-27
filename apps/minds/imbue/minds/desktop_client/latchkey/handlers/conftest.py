"""Fixtures shared by the latchkey handler tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.minds.desktop_client.testing import StaticPendingRequests
from imbue.minds.desktop_client.testing import desktop_state_app_context


@pytest.fixture(autouse=True)
def handler_state_context(tmp_path: Path) -> Iterator[StaticPendingRequests]:
    """An ambient DesktopClientState app context for direct handler-method calls.

    The shared resolve epilogue reads ``get_state()``; tests that drive a
    handler through the full desktop client push their own request context on
    top of this one, so both styles coexist. Yields the ambient view for
    assertions in direct-call tests.
    """
    with desktop_state_app_context(tmp_path) as view:
        yield view
