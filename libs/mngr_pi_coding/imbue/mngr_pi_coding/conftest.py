import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """A real local-provider Host for tests that exercise actual host shell commands.

    Needed where the code under test issues compound shell commands (e.g. the shared
    `symlink_on_host` helper runs `mkdir -p ... && ln -sfn ...`): the lightweight `FakeHost`
    runs commands via `shlex.split` with no shell, so `&&` and shell builtins do not work.
    """
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))
