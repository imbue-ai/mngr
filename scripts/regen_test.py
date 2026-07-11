import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


# Regenerating every artifact loads all plugins in a subprocess and runs `uv export`,
# which takes ~20s; the default 10s per-test timeout is not enough for this drift guard.
@pytest.mark.timeout(120)
def test_generated_artifacts_are_current() -> None:
    """Every code-derived artifact is committed up to date (CLI docs, capability matrix, constraints).

    Drives the umbrella regenerator's ``--check`` in a clean subprocess so loading
    all plugins / building the capability registry does not leak global state into
    other tests. When this fails, regenerate with ``just regenerate``.
    """
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "regen.py"), "--check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Generated artifacts are out of date; run `just regenerate`.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
