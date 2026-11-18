import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.imbue_common.ratchet_testing.ratchets import check_no_type_errors


def _project_whose_environment_a_sync_would_change(project_dir: Path, source: str) -> Path:
    """A uv project declaring a dependency its environment lacks, so any lock or sync must reach an index."""
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "type-check-target"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["imbue-absent-dependency-{uuid4().hex}"]\n'
    )
    (project_dir / "module.py").write_text(source)
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(project_dir / ".venv")],
        check=True,
        capture_output=True,
    )
    (project_dir / ".venv" / "bin" / "ty").symlink_to(Path(sys.executable).parent / "ty")
    return project_dir


@pytest.mark.usefixtures("offline_uv")
def test_check_no_type_errors_checks_the_environment_as_it_is_without_locking_or_syncing(tmp_path: Path) -> None:
    project = _project_whose_environment_a_sync_would_change(
        tmp_path / "project", "def double(value: int) -> int:\n    return value * 2\n"
    )

    check_no_type_errors(project)

    assert not (project / "uv.lock").exists()


@pytest.mark.usefixtures("offline_uv")
def test_check_no_type_errors_reports_a_type_error_in_a_project_it_does_not_sync(tmp_path: Path) -> None:
    project = _project_whose_environment_a_sync_would_change(
        tmp_path / "project", "def double(value: int) -> int:\n    return str(value)\n"
    )

    with pytest.raises(AssertionError, match=r"module\.py:2:\d+: error\[invalid-return-type\]"):
        check_no_type_errors(project)
