from pathlib import Path

import pytest


@pytest.fixture
def source_and_work_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create source and work directories for work_dir_extra_paths tests.

    Returns (source_dir, work_dir), both already created under tmp_path.
    """
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return (source_dir, work_dir)
