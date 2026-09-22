"""Test fixtures for mngr-file.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.) and defines file-specific fixtures below.
"""

from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pluggy
import pytest

from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_file.testing import AddressedMachineFactory
from imbue.mngr_file.testing import StoppedHost
from imbue.mngr_file.testing import StoppedHostFactory
from imbue.mngr_file.testing import StoppedHostStorage
from imbue.mngr_file.testing import make_stopped_host_factory
from imbue.mngr_file.testing import registered_stopped_host_backend

register_plugin_test_fixtures(globals())


@pytest.fixture
def stopped_host_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_manager: pluggy.PluginManager,
) -> Generator[StoppedHostFactory, None, None]:
    """A factory for hosts that are stopped, discoverable through the CLI, until started."""
    with registered_stopped_host_backend():
        yield make_stopped_host_factory(tmp_path / "stopped_hosts", monkeypatch)


@pytest.fixture
def stopped_host(stopped_host_factory: StoppedHostFactory) -> StoppedHost:
    """A stopped host whose persisted storage can be reached, and fails as a filesystem does."""
    return stopped_host_factory.create(StoppedHostStorage.FILESYSTEM)


@pytest.fixture
def service_stopped_host(stopped_host_factory: StoppedHostFactory) -> StoppedHost:
    """A stopped host whose persisted storage can be reached, and fails as a storage service does."""
    return stopped_host_factory.create(StoppedHostStorage.SERVICE)


@pytest.fixture
def unreachable_stopped_host(stopped_host_factory: StoppedHostFactory) -> StoppedHost:
    """A stopped host whose persisted storage cannot be reached."""
    return stopped_host_factory.create(StoppedHostStorage.UNREACHABLE)


@pytest.fixture
def local_agent(local_host: Host, temp_work_dir: Path) -> AgentInterface:
    """An agent recorded on the local host, with ``temp_work_dir`` as its work directory, not started."""
    return create_test_agent_state(local_host, temp_work_dir, f"file-agent-{uuid4().hex}")


@pytest.fixture
def addressed_machine_factory(
    temp_host_dir: Path, stopped_host_factory: StoppedHostFactory
) -> AddressedMachineFactory:
    """A factory for the machines a command can address: the running local host, or a stopped host."""
    return AddressedMachineFactory(local_host_dir=temp_host_dir, stopped_host_factory=stopped_host_factory)
