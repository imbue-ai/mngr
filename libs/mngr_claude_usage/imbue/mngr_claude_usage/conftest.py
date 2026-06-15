import importlib.resources
from pathlib import Path

import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_claude_usage import resources as _resources

register_plugin_test_fixtures(globals())

WRITER_SCRIPT_NAME = "claude_usage_writer.sh"


@pytest.fixture
def local_host(local_provider: LocalProviderInstance) -> Host:
    """Local-provider Host for tests that exercise host.read_text_file / host.write_file."""
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))


@pytest.fixture
def writer_path(tmp_path: Path) -> Path:
    """Stage the writer script onto disk with execute bit, ready for subprocess."""
    src = importlib.resources.files(_resources).joinpath(WRITER_SCRIPT_NAME)
    dst = tmp_path / WRITER_SCRIPT_NAME
    dst.write_bytes(src.read_bytes())
    dst.chmod(0o755)
    return dst


@pytest.fixture
def events_file(tmp_path: Path) -> Path:
    return tmp_path / "events" / "claude" / "usage" / "events.jsonl"
