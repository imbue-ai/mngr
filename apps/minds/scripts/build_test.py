"""Tests for the bundling contract that ``apps/minds/scripts/build.js`` relies on.

The build script calls ``uv build --package <name> --wheel`` for each workspace
package it ships. Two properties are load-bearing:

- Every bundled wheel must be pure Python (``py3-none-any``), because the
  build is single-platform and the wheel is shipped to all platforms as-is.
- Every bundled wheel must exclude test files, because the packaged app has
  no use for them and shipping them bloats the bundle / exposes test-only
  imports to the runtime.

These tests run ``uv build`` for each workspace package listed in
``WORKSPACE_PACKAGES`` (must mirror the list in ``build.js``) and assert
both properties.
"""

import re
import subprocess
import zipfile
from pathlib import Path

import pytest

# Must mirror WORKSPACE_PACKAGES in apps/minds/scripts/build.js.
WORKSPACE_PACKAGES = [
    "minds",
    "imbue-mngr",
    "imbue-mngr-claude",
    "imbue-mngr-modal",
    "imbue-common",
    "concurrency-group",
    "resource-guards",
    "modal-proxy",
]

MONOREPO_ROOT = Path(__file__).resolve().parents[3]
TEST_PATTERN = re.compile(r"(^|/)(test_[^/]*\.py|[^/]+_test\.py|conftest\.py)$")


@pytest.fixture(scope="module")
def built_wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build every workspace package wheel once, share across tests in the module."""
    out_dir = tmp_path_factory.mktemp("wheels")
    wheels: dict[str, Path] = {}
    for name in WORKSPACE_PACKAGES:
        before = set(out_dir.iterdir())
        subprocess.run(
            ["uv", "build", "--package", name, "--wheel", "--out-dir", str(out_dir)],
            cwd=MONOREPO_ROOT,
            check=True,
            capture_output=True,
        )
        produced = [p for p in out_dir.iterdir() if p not in before and p.suffix == ".whl"]
        assert len(produced) == 1, f"Expected exactly one wheel for {name}, got {produced}"
        wheels[name] = produced[0]
    return wheels


@pytest.mark.parametrize("package_name", WORKSPACE_PACKAGES)
def test_workspace_wheel_is_pure_python(built_wheels: dict[str, Path], package_name: str) -> None:
    """Every workspace wheel must be tagged py3-none-any.

    If this ever fails, a workspace package has picked up a C extension or a
    Python-version-specific dependency. The wheel-bundling strategy assumes
    pure Python so one build ships to all platforms. Adding native code
    requires per-platform wheel builds (see build.js).
    """
    whl = built_wheels[package_name]
    # Wheel filename convention: {distribution}-{version}-{python tag}-{abi tag}-{platform tag}.whl
    parts = whl.stem.split("-")
    assert parts[-3:] == ["py3", "none", "any"], (
        f"{whl.name} is not pure Python. Expected tags py3-none-any, got {parts[-3:]}. "
        "Workspace packages must be pure Python for the current bundling strategy."
    )


@pytest.mark.parametrize("package_name", WORKSPACE_PACKAGES)
def test_workspace_wheel_excludes_test_files(built_wheels: dict[str, Path], package_name: str) -> None:
    """Wheels must not contain test files.

    If this fails, the package's ``pyproject.toml`` is missing or has wrong
    `exclude` rules under `[tool.hatch.build.targets.wheel]`. See e.g.
    ``libs/imbue_common/pyproject.toml`` for the expected pattern.
    """
    with zipfile.ZipFile(built_wheels[package_name]) as zf:
        leaks = [n for n in zf.namelist() if TEST_PATTERN.search(n)]
    assert leaks == [], (
        f"{built_wheels[package_name].name} contains test files that should be excluded: {leaks}. "
        'Add `exclude = ["*_test.py", "test_*.py", "**/conftest.py"]` to '
        "[tool.hatch.build.targets.wheel] in the package's pyproject.toml."
    )


def test_build_js_workspace_packages_match() -> None:
    """Guard against drift between the test's WORKSPACE_PACKAGES list and build.js."""
    build_js = (Path(__file__).parent / "build.js").read_text()
    for name in WORKSPACE_PACKAGES:
        assert f"'{name}'" in build_js or f'"{name}"' in build_js, (
            f"Package {name!r} is in the test's WORKSPACE_PACKAGES but not found in build.js. "
            "Update one side or the other to keep them in sync."
        )
