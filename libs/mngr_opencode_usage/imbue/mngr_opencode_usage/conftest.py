import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Local-provider Host for tests that exercise host.write_text_file."""
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))
