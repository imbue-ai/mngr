from pathlib import Path
from typing import Any

import app
import pytest
import yaml


@pytest.fixture
def litellm_proxy_config_path(tmp_path: Path) -> str:
    """Path to the litellm config written by the real ``_write_config_file`` writer.

    Redirects the write to a per-test temp path (instead of the deployed default
    ``/tmp/litellm_config.yaml``) so tests never share on-disk state, then hands
    back the path for the test to read and parse exactly as the proxy would.
    """
    return app._write_config_file(str(tmp_path / "litellm_config.yaml"))


@pytest.fixture
def litellm_proxy_config(litellm_proxy_config_path: str) -> Any:
    """The litellm config the proxy wrote, parsed back exactly as the proxy would.

    Parses the on-disk YAML the writer produced so tests assert against the config
    the proxy actually boots from rather than the in-memory ``LITELLM_CONFIG`` dict.
    Returns ``Any`` (what ``yaml.safe_load`` yields) so tests can subscript the
    heterogeneous mapping without the type checker rejecting the indexing.
    """
    return yaml.safe_load(Path(litellm_proxy_config_path).read_text())
