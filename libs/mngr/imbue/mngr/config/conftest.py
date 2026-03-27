from pathlib import Path

import pytest


@pytest.fixture
def completion_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set MNGR_COMPLETION_CACHE_DIR to a temporary directory."""
    monkeypatch.setenv("MNGR_COMPLETION_CACHE_DIR", str(tmp_path))
    return tmp_path
