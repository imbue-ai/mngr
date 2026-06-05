from pathlib import Path

import app
import pytest


@pytest.fixture
def litellm_proxy_config_path(tmp_path: Path) -> str:
    """Path to the litellm config written by the real ``_write_config_file`` writer.

    Redirects the write to a per-test temp path (instead of the deployed default
    ``/tmp/litellm_config.yaml``) so tests never share on-disk state, then hands
    back the path for the test to read and parse exactly as the proxy would.
    """
    return app._write_config_file(str(tmp_path / "litellm_config.yaml"))
