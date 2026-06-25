"""Test fixtures for mngr-test-mapreduce.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.) and defines test-mapreduce-specific fixtures below.
"""

from collections.abc import Iterator

import pytest

from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_tmr.report import reset_outcome_caches

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def _reset_report_outcome_caches() -> Iterator[None]:
    """Isolate the report module's process-global outcome caches per test.

    The caches key parsed outcomes by ``AgentName`` and ignore ``output_dir``,
    so without this fixture tests that reuse an agent name (across distinct temp
    dirs) would read each other's cached outcomes in an order-dependent way.
    """
    reset_outcome_caches()
    yield
    reset_outcome_caches()


@pytest.fixture
def localhost(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    """Get a started localhost for tests that need to read/write files on a host."""
    host, _ = ensure_host_started(
        local_provider.get_host(HostName(LOCAL_HOST_NAME)), is_start_desired=True, provider=local_provider
    )
    return host
