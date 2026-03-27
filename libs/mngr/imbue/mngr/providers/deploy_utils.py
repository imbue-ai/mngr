"""Shared utilities for provider get_files_for_deploy implementations."""

import importlib.metadata
import json
from enum import auto
from pathlib import Path

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError


class MngrInstallMode(UpperCaseStrEnum):
    """Controls how mngr is made available in a target environment.

    Used by mngr_schedule (for image building) and mngr_recursive (for
    provisioning-time injection).

    AUTO: Detect automatically based on how mngr is currently installed locally.
    PACKAGE: Install from PyPI via uv tool install / pip install.
    EDITABLE: Package the local source tree and install it in editable mode.
    SKIP: Do not install mngr (assume it is already available).
    """

    AUTO = auto()
    PACKAGE = auto()
    EDITABLE = auto()
    SKIP = auto()


def detect_mngr_install_mode(package_name: str = "mngr") -> MngrInstallMode:
    """Detect whether a package is installed in editable or package mode.

    Checks the package's direct_url.json metadata (PEP 610) for the
    "editable" flag. Returns EDITABLE if the package is installed in
    development mode, PACKAGE otherwise.
    """
    try:
        dist = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError:
        return MngrInstallMode.PACKAGE

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is not None:
        try:
            direct_url = json.loads(direct_url_text)
        except (json.JSONDecodeError, AttributeError):
            return MngrInstallMode.PACKAGE
        if direct_url.get("dir_info", {}).get("editable", False):
            return MngrInstallMode.EDITABLE
    return MngrInstallMode.PACKAGE


def resolve_mngr_install_mode(mode: MngrInstallMode, package_name: str = "mngr") -> MngrInstallMode:
    """Resolve AUTO mode to a concrete install mode, or pass through others."""
    if mode == MngrInstallMode.AUTO:
        resolved = detect_mngr_install_mode(package_name)
        logger.info("Auto-detected mngr install mode: {}", resolved.value.lower())
        return resolved
    return mode


def collect_provider_profile_files(
    mngr_ctx: MngrContext,
    provider_name: str,
    excluded_file_names: frozenset[str],
) -> dict[Path, Path | str]:
    """Collect non-secret files from a provider's profile directory for deployment.

    Scans the provider's subdirectory under the profile (e.g.
    ~/.mngr/profiles/<id>/providers/<provider_name>/) and returns all files
    except those whose names appear in excluded_file_names (typically SSH
    keypairs and known_hosts).

    Returns dict mapping destination paths (starting with "~/") to local
    source paths.
    """
    files: dict[Path, Path | str] = {}
    provider_dir = mngr_ctx.profile_dir / "providers" / provider_name
    if not provider_dir.is_dir():
        return files

    user_home = Path.home()
    for file_path in provider_dir.rglob("*"):
        if file_path.is_file() and file_path.name not in excluded_file_names:
            relative = file_path.relative_to(user_home)
            files[Path(f"~/{relative}")] = file_path
    return files


def collect_deploy_files(
    mngr_ctx: MngrContext,
    repo_root: Path,
    include_user_settings: bool = True,
    include_project_settings: bool = True,
) -> dict[Path, Path | str]:
    """Collect all files for deployment by calling the get_files_for_deploy hook.

    Calls the get_files_for_deploy hook on all registered plugins and merges
    the results into a single dict. Used by both mngr_schedule (for image building)
    and mngr_recursive (for provisioning-time injection).

    Destination paths must either start with "~" (user home files) or be
    relative paths (project files). Absolute paths that do not start with
    "~" are rejected with an MngrError.
    """
    all_results: list[dict[Path, Path | str]] = mngr_ctx.pm.hook.get_files_for_deploy(
        mngr_ctx=mngr_ctx,
        include_user_settings=include_user_settings,
        include_project_settings=include_project_settings,
        repo_root=repo_root,
    )
    merged: dict[Path, Path | str] = {}
    for result in all_results:
        for dest_path, source in result.items():
            dest_str = str(dest_path)
            if dest_str.startswith("/"):
                raise MngrError(f"Deploy file destination path must be relative or start with '~', got: {dest_path}")
            if dest_path in merged:
                logger.warning(
                    "Deploy file collision: {} registered by multiple plugins, overwriting previous value",
                    dest_path,
                )
            merged[dest_path] = source
    return merged
