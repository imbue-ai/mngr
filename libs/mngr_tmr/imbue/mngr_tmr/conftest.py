"""Test fixtures for mngr-test-mapreduce.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.) and defines test-mapreduce-specific fixtures below.
"""

import pytest

from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture
def localhost(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    """Get a started localhost for tests that need to read/write files on a host."""
    host, _ = ensure_host_started(
        local_provider.get_host(HostName(LOCAL_HOST_NAME)), is_start_desired=True, provider=local_provider
    )
    return host
