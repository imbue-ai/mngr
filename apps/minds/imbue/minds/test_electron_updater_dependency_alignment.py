"""Verify the desktop app's direct ``builder-util-runtime`` pin tracks electron-updater.

The Electron shell pins ``electron-updater`` exactly and also lists its
``builder-util-runtime`` dependency as a direct dependency, pinned to the same
version electron-updater resolved against. ``@todesktop/runtime`` pulls in an
old electron-updater with an old builder-util-runtime, and ToDesktop's Linux
build packs electron-updater without its nested copy, so the top-level copy is
the one electron-updater actually runs against. The direct pin decides which
copy that is.

That only helps while the pin equals what the lockfile records under the
pinned electron-updater's own dependencies. If electron-updater moves to a
newer builder-util-runtime and the pin stays behind, every Linux update
download finishes and then fails at electron-updater's final temp-file rename
with ``retry is not a function``, while CI stays green. This test reads both
pins and the lockfile and asserts they agree.
"""

import json
import re
from pathlib import Path
from typing import Final

_APP_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_PACKAGE_JSON_PATH: Final[Path] = _APP_ROOT / "package.json"
_PNPM_LOCK_PATH: Final[Path] = _APP_ROOT / "pnpm-lock.yaml"

_EXACT_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")


def _read_exact_dependency_pin(dependencies: dict[str, str], name: str) -> str:
    """Return the version ``name`` is pinned to, requiring an exact ``X.Y.Z`` pin."""
    assert name in dependencies, (
        f"{_PACKAGE_JSON_PATH} no longer lists {name!r} under dependencies. If the updater "
        "stack changed, update or delete this test with it."
    )
    specifier = dependencies[name]
    assert _EXACT_VERSION_PATTERN.match(specifier), (
        f"{name!r} in {_PACKAGE_JSON_PATH} is pinned as {specifier!r}; it must be an exact "
        "X.Y.Z version so the copy ToDesktop hoists is the one the lockfile was resolved with."
    )
    return specifier


def _read_locked_builder_util_runtime_for(electron_updater_version: str, lockfile_text: str) -> str:
    """Return the builder-util-runtime version the lockfile resolved for electron-updater.

    The lockfile lists ``electron-updater@<version>`` twice: once under
    ``packages:`` (with ``resolution:``) and once under ``snapshots:`` (with
    ``dependencies:``). Only the latter names its resolved dependencies, so the
    match anchors on the ``dependencies:`` line directly under the key.
    """
    snapshot_pattern = re.compile(
        rf"^  electron-updater@{re.escape(electron_updater_version)}:\n    dependencies:\n((?:      .*\n)+)",
        flags=re.MULTILINE,
    )
    snapshot_match = snapshot_pattern.search(lockfile_text)
    assert snapshot_match is not None, (
        f"{_PNPM_LOCK_PATH} has no snapshot for electron-updater@{electron_updater_version} with a "
        "dependencies block. Refresh the lockfile (`pnpm install --lockfile-only`) after changing "
        "the electron-updater pin."
    )
    dependency_match = re.search(r"^      builder-util-runtime: (\S+)$", snapshot_match.group(1), flags=re.MULTILINE)
    assert dependency_match is not None, (
        f"electron-updater@{electron_updater_version} in {_PNPM_LOCK_PATH} no longer depends on "
        "builder-util-runtime. If electron-updater dropped it, drop the direct pin in package.json "
        "and this test with it."
    )
    return dependency_match.group(1)


def test_direct_builder_util_runtime_pin_matches_the_locked_electron_updater_dependency() -> None:
    """``builder-util-runtime`` in package.json must equal what electron-updater resolved against."""
    dependencies = json.loads(_PACKAGE_JSON_PATH.read_text())["dependencies"]
    electron_updater_version = _read_exact_dependency_pin(dependencies, "electron-updater")
    direct_pin = _read_exact_dependency_pin(dependencies, "builder-util-runtime")

    locked_version = _read_locked_builder_util_runtime_for(electron_updater_version, _PNPM_LOCK_PATH.read_text())

    assert direct_pin == locked_version, (
        f"package.json pins builder-util-runtime {direct_pin}, but electron-updater "
        f"{electron_updater_version} resolved against builder-util-runtime {locked_version} in "
        f"{_PNPM_LOCK_PATH}. ToDesktop's Linux build runs electron-updater against the top-level "
        "copy, so a mismatch makes every Linux update download fail at the final rename with "
        "`retry is not a function`. Set the direct pin to the locked version and refresh the lockfile."
    )
