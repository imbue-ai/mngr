import importlib.resources
from pathlib import Path

import pytest

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_claude_usage import resources as _resources

register_plugin_test_fixtures(globals())

WRITER_SCRIPT_NAME = "claude_usage_writer.sh"


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
