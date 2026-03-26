"""Test fixtures for mng-test-mapreduce.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, etc.) and defines test-mapreduce-specific fixtures below.
"""

import pytest

from imbue.mng.api.find import ensure_host_started
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture
def localhost(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    """Get a started localhost for tests that need to read/write files on a host."""
    host, _ = ensure_host_started(
        local_provider.get_host(HostName("localhost")), is_start_desired=True, provider=local_provider
    )
    return host
