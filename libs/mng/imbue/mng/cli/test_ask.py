"""Release tests for the ask command's source directory detection.

These tests install mng from PyPI into a temporary virtualenv and verify
that the installed package layout is compatible with _find_source_directories.
"""

import json
import subprocess
import textwrap
import venv
from pathlib import Path

import pytest

from imbue.mng.cli.ask import _MONOREPO_PACKAGE_DIRS


@pytest.mark.acceptance
def test_installed_package_layout_detected_correctly(tmp_path: Path) -> None:
    """In a pip-installed env, source detection should return [site-packages/imbue/].

    Installs mng and all published plugins from PyPI into a temporary venv,
    then runs the same detection logic that _find_source_directories uses
    to verify it correctly identifies the installed layout.
    """
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    pip = str(venv_dir / "bin" / "pip")
    python = str(venv_dir / "bin" / "python3")

    # Install mng and all published plugins from PyPI.
    pypi_names = [d.replace("_", "-") for d in _MONOREPO_PACKAGE_DIRS]
    subprocess.run([pip, "install", *pypi_names], check=True, capture_output=True)

    # Run the detection logic inside the installed venv.  We inline the
    # logic rather than importing _find_source_directories because the
    # PyPI version may not have it yet.
    script = textwrap.dedent("""\
        import json
        from pathlib import Path

        ask_file = Path(__import__("imbue.mng.cli.ask", fromlist=["ask"]).__file__).resolve()
        imbue_dir = ask_file.parents[2]
        project_root = imbue_dir.parent

        has_pyproject = (project_root / "pyproject.toml").is_file()
        has_mng_subdir = (imbue_dir / "mng").is_dir()

        print(json.dumps({
            "imbue_dir": str(imbue_dir),
            "project_root": str(project_root),
            "has_pyproject": has_pyproject,
            "has_mng_subdir": has_mng_subdir,
            "imbue_contents": sorted(
                p.name for p in imbue_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ),
        }))
    """)
    result = subprocess.run(
        [python, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(result.stdout)

    # Should NOT be detected as a source checkout.
    assert not info["has_pyproject"], (
        f"Installed env incorrectly detected as source checkout: {info['project_root']}"
    )

    # Should detect the imbue/ directory.
    assert info["has_mng_subdir"], "imbue/mng/ not found in installed layout"

    # All published packages should be present under imbue/.
    imbue_contents = set(info["imbue_contents"])
    for pkg_dir in _MONOREPO_PACKAGE_DIRS:
        assert pkg_dir in imbue_contents, f"Missing {pkg_dir} in installed imbue/ directory"
