"""Verify that Docker scripts shared with imbue-ai/keystone stay in sync.

These scripts are copied from the keystone repo
(keystone/src/keystone/modal/) into this repo
(libs/mngr/imbue/mngr/resources/). This test fetches the canonical
versions from GitHub and fails if the local copies have diverged.
"""

from pathlib import Path
from urllib.request import urlopen

import pytest

_RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources"

_SHARED_SCRIPTS = [
    "start_dockerd.sh",
    "wait_for_docker.sh",
]

_RAW_URL_BASE = "https://raw.githubusercontent.com/imbue-ai/keystone/main/keystone/src/keystone/modal"


@pytest.mark.acceptance
@pytest.mark.parametrize("script", _SHARED_SCRIPTS)
def test_shared_script_matches_keystone(script: str) -> None:
    local_path = _RESOURCES_DIR / script
    assert local_path.exists(), f"Local script missing: {local_path}"

    local_content = local_path.read_text()

    url = f"{_RAW_URL_BASE}/{script}"
    with urlopen(url, timeout=10) as resp:
        remote_content = resp.read().decode()

    assert local_content == remote_content, (
        f"resources/{script} has diverged from imbue-ai/keystone. "
        f"Update the local copy or the keystone version to match."
    )
