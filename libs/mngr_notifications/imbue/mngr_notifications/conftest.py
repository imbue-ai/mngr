from collections.abc import Generator

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture()
def notification_cg() -> Generator[ConcurrencyGroup, None, None]:
    """ConcurrencyGroup for notification subprocess calls."""
    with ConcurrencyGroup(name="test-notification") as group:
        yield group
