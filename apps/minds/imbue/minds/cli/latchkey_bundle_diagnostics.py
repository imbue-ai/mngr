"""Diagnostics for a bundled latchkey CLI that fails to initialize.

Compares the installed bundle with the ``manifest.json`` that
``scripts/bundle-manifest.js`` writes at build time, to tell a bundle damaged
after install from one that shipped broken. Change the manifest format on both
sides together.
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

LATCHKEY_BUNDLE_MANIFEST_FILENAME: Final[str] = "manifest.json"

# The log line rides along as a Sentry breadcrumb, so it names only the first
# few paths of each kind.
_MAX_LISTED_PATHS: Final[int] = 10


class LatchkeyBundleIntegrity(FrozenModel):
    """How an installed latchkey bundle differs from the manifest written at build time.

    Paths are POSIX paths relative to the bundle root, sorted.
    """

    manifest_file_count: int = Field(description="Number of files the manifest records")
    missing_paths: tuple[str, ...] = Field(description="Files in the manifest that are not on disk")
    mismatched_paths: tuple[str, ...] = Field(
        description="Files on disk whose SHA-256 differs from the manifest (files recorded without a hash never are)"
    )
    unexpected_paths: tuple[str, ...] = Field(description="Files on disk that the manifest does not record")


def check_latchkey_bundle_integrity(bundle_dir: Path) -> LatchkeyBundleIntegrity | None:
    """Compare the files under ``bundle_dir`` with its build-time manifest, or return ``None`` if it has none."""
    manifest_path = bundle_dir / LATCHKEY_BUNDLE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    expected_sha256_by_path: dict[str, str | None] = json.loads(manifest_path.read_text())["files"]
    actual_paths = {
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    mismatched_paths: list[str] = []
    for relative_path in sorted(actual_paths & expected_sha256_by_path.keys()):
        expected_sha256 = expected_sha256_by_path[relative_path]
        # Mach-O files are recorded without a hash: macOS packaging re-signs
        # them after the build, so only their presence is checked.
        if expected_sha256 is None:
            continue
        with (bundle_dir / relative_path).open("rb") as file:
            actual_sha256 = hashlib.file_digest(file, "sha256").hexdigest()
        if actual_sha256 != expected_sha256:
            mismatched_paths.append(relative_path)
    return LatchkeyBundleIntegrity(
        manifest_file_count=len(expected_sha256_by_path),
        missing_paths=tuple(sorted(expected_sha256_by_path.keys() - actual_paths)),
        mismatched_paths=tuple(mismatched_paths),
        unexpected_paths=tuple(sorted(actual_paths - expected_sha256_by_path.keys())),
    )


def _read_bundled_latchkey_version(bundle_dir: Path) -> str | None:
    package_json_path = bundle_dir / "node_modules" / "latchkey" / "package.json"
    if not package_json_path.is_file():
        return None
    version = json.loads(package_json_path.read_text()).get("version")
    return version if isinstance(version, str) else None


def _describe_bundled_latchkey_version(bundle_dir: Path) -> str:
    try:
        return str(_read_bundled_latchkey_version(bundle_dir))
    except (OSError, ValueError, AttributeError) as e:
        return f"unreadable ({type(e).__name__}: {e})"


def _first_paths(paths: tuple[str, ...]) -> list[str]:
    return list(paths[:_MAX_LISTED_PATHS])


def log_latchkey_bundle_diagnostics(latchkey_binary: str) -> None:
    """Log the version and integrity of the latchkey bundle behind ``latchkey_binary``.

    Logs at WARNING so the line reaches the crash's Sentry event as a breadcrumb
    rather than becoming an event of its own. Never raises over the error being
    diagnosed: a bundle it cannot read is reported in the line instead.
    """
    # Resolve a bare name through PATH, and stop at a binary that does not
    # exist: walking up from it would land on the current directory.
    resolved_binary = shutil.which(latchkey_binary) or (latchkey_binary if Path(latchkey_binary).is_file() else None)
    if resolved_binary is None:
        logger.warning(
            "Latchkey binary {} failed to initialize; it does not exist, so there is no bundle to check",
            latchkey_binary,
        )
        return
    # The shim sits at <bundle>/bin/latchkey.
    bundle_dir = Path(resolved_binary).parent.parent
    bundled_version = _describe_bundled_latchkey_version(bundle_dir)
    try:
        integrity = check_latchkey_bundle_integrity(bundle_dir)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning(
            "Latchkey binary {} failed to initialize; bundled latchkey version {}; bundle integrity check failed: {}: {}",
            latchkey_binary,
            bundled_version,
            type(e).__name__,
            e,
        )
        return
    if integrity is None:
        logger.warning(
            "Latchkey binary {} failed to initialize; bundled latchkey version {}; no {} to check the bundle against",
            latchkey_binary,
            bundled_version,
            LATCHKEY_BUNDLE_MANIFEST_FILENAME,
        )
        return
    logger.warning(
        "Latchkey binary {} failed to initialize; bundled latchkey version {}; bundle integrity against its "
        "{}-file manifest: {} missing, {} mismatched, {} unexpected; "
        "first missing {}, first mismatched {}, first unexpected {}",
        latchkey_binary,
        bundled_version,
        integrity.manifest_file_count,
        len(integrity.missing_paths),
        len(integrity.mismatched_paths),
        len(integrity.unexpected_paths),
        _first_paths(integrity.missing_paths),
        _first_paths(integrity.mismatched_paths),
        _first_paths(integrity.unexpected_paths),
    )
