"""Verify that scripts shared with imbue-ai/code-guardian stay in sync.

These scripts are duplicated between this repo (scripts/) and the
imbue-code-guardian plugin (plugins/imbue-code-guardian/scripts/) in
the code-guardian repo. This test fetches the canonical versions from
GitHub and fails if the local copies have diverged.
"""

from pathlib import Path
from urllib.request import urlopen

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]

_SHARED_SCRIPTS = [
    "config_utils.sh",
    "stop_hook_gates.sh",
    "export_transcript_paths.sh",
    "filter_transcript.py",
]

_RAW_URL_BASE = "https://raw.githubusercontent.com/imbue-ai/code-guardian/main/plugins/imbue-code-guardian/scripts"


@pytest.mark.acceptance
@pytest.mark.parametrize("script", _SHARED_SCRIPTS)
def test_shared_script_matches_code_guardian(script: str) -> None:
    local_path = _REPO_ROOT / "scripts" / script
    assert local_path.exists(), f"Local script missing: {local_path}"

    local_content = local_path.read_text()

    url = f"{_RAW_URL_BASE}/{script}"
    with urlopen(url, timeout=10) as resp:
        remote_content = resp.read().decode()

    assert local_content == remote_content, (
        f"scripts/{script} has diverged from imbue-ai/code-guardian. "
        f"Update the local copy or the code-guardian version to match."
    )
