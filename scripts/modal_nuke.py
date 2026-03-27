#!/usr/bin/env python3
"""Nuke all Modal resources for this mngr installation.

Use this when mngr state gets out of sync with Modal -- for example, when
<host_id>.json files on the Modal volume are outdated and `mngr destroy`
no longer works.

This script bypasses mngr entirely and uses the Modal CLI directly.

It will:
  1. Stop all Modal apps in the mngr environment
  2. Delete all Modal volumes in the mngr environment

Usage:
    uv run python scripts/modal_nuke.py
    uv run python scripts/modal_nuke.py --dry-run
    uv run python scripts/modal_nuke.py -e mngr-<user_id>
"""

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import setup_logging

DEFAULT_MNGR_DIR: Final[Path] = Path("~/.mngr")
DEFAULT_PREFIX: Final[str] = "mngr-"


def _get_app_id(app: Mapping[str, str]) -> str:
    return app.get("App ID", app.get("app_id", app.get("id", "unknown")))


def _get_volume_name(volume: Mapping[str, str]) -> str:
    return volume.get("Name", volume.get("name", "unknown"))


def _read_user_id(mngr_dir: Path) -> str | None:
    """Read the user_id from the mngr profile directory."""
    config_path = mngr_dir / "config.toml"
    if not config_path.exists():
        return None
    try:
        with config_path.open("rb") as f:
            root_config = tomllib.load(f)
        profile_id = root_config.get("profile")
        if not profile_id:
            return None
        user_id_path = mngr_dir / "profiles" / profile_id / "user_id"
        if user_id_path.exists():
            return user_id_path.read_text().strip()
    except (tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning(f"Failed to read config: {exc}")
    return None


def _detect_environment(mngr_dir: Path, prefix: str) -> str | None:
    user_id = _read_user_id(mngr_dir=mngr_dir)
    if user_id:
        return f"{prefix}{user_id}"
    return None


def _run_modal(args: Sequence[str], environment: str | None) -> subprocess.CompletedProcess[str]:
    cmd = ["modal", *args]
    if environment:
        cmd.extend(["-e", environment])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _list_resources(resource_type: str, environment: str) -> list[dict[str, str]] | None:
    """List all resources of the given type (e.g. 'app', 'volume') in the environment.

    Returns None on failure (CLI error or unparseable output), empty list if no resources found.
    """
    result = _run_modal([resource_type, "list", "--json"], environment)
    if result.returncode != 0:
        logger.error(f"Failed to list {resource_type}s: {result.stderr.strip()}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse {resource_type} list JSON: {exc}")
        return None


def _stop_app(app_id: str, environment: str) -> tuple[bool, str]:
    """Stop a Modal app. Returns (success, stderr)."""
    result = _run_modal(["app", "stop", app_id], environment)
    return result.returncode == 0, result.stderr.strip()


def _delete_volume(volume_name: str, environment: str) -> tuple[bool, str]:
    """Delete a Modal volume. Returns (success, stderr)."""
    result = _run_modal(["volume", "delete", volume_name, "-y"], environment)
    return result.returncode == 0, result.stderr.strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nuke all Modal resources for this mngr installation. "
        "Use when mngr state is out of sync with Modal.",
    )
    parser.add_argument(
        "--environment",
        "-e",
        help="Modal environment name (auto-detected from ~/.mngr profile if not specified)",
    )
    parser.add_argument(
        "--mngr-dir",
        type=Path,
        default=DEFAULT_MNGR_DIR,
        help=f"Path to mngr directory (default: {DEFAULT_MNGR_DIR})",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"Mngr prefix (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without actually deleting",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Skip confirmation prompt",
    )
    return parser.parse_args()


def _resolve_environment(args: argparse.Namespace) -> str | None:
    mngr_dir = args.mngr_dir.expanduser()
    return args.environment or _detect_environment(mngr_dir, args.prefix)


def _display_resources(apps: Sequence[Mapping[str, str]], volumes: Sequence[Mapping[str, str]]) -> None:
    if apps:
        logger.info(f"Apps to stop ({len(apps)}):")
        for app in apps:
            description = app.get("Description", app.get("description", app.get("name", "")))
            logger.info(f"  {_get_app_id(app)}  {description}")
    else:
        logger.info("No apps found.")

    if volumes:
        logger.info(f"Volumes to delete ({len(volumes)}):")
        for volume in volumes:
            logger.info(f"  {_get_volume_name(volume)}")
    else:
        logger.info("No volumes found.")


def _confirm_nuke(is_force: bool) -> bool:
    if is_force:
        return True
    response = input("Proceed with nuke? [y/N] ")
    return response.lower() in ("y", "yes")


def _nuke_resources(
    resources: Sequence[Mapping[str, str]],
    action_label: str,
    get_identifier: Callable[[Mapping[str, str]], str],
    execute_action: Callable[[str, str], tuple[bool, str]],
    environment: str,
) -> int:
    """Execute a nuke action on a list of resources. Returns the number of failures."""
    failure_count = 0
    for resource in resources:
        identifier = get_identifier(resource)
        print(f"{action_label} {identifier}...", end=" ", flush=True)
        is_success, stderr = execute_action(identifier, environment)
        if is_success:
            print("done")
        else:
            failure_count += 1
            print(stderr if stderr else "FAILED")
    return failure_count


def _execute_nuke(apps: Sequence[Mapping[str, str]], volumes: Sequence[Mapping[str, str]], environment: str) -> int:
    app_failures = _nuke_resources(apps, "Stopping app", _get_app_id, _stop_app, environment)
    volume_failures = _nuke_resources(volumes, "Deleting volume", _get_volume_name, _delete_volume, environment)
    return app_failures + volume_failures


def main() -> int:
    args = _parse_args()
    setup_logging(level="INFO")

    if not shutil.which("modal"):
        logger.error("modal CLI not found. Install it with: pip install modal")
        return 1

    environment = _resolve_environment(args)
    if environment is None:
        logger.error("Could not auto-detect Modal environment. Use --environment to specify it explicitly.")
        return 1

    logger.info(f"Modal environment: {environment}")

    apps = _list_resources("app", environment)
    volumes = _list_resources("volume", environment)

    if apps is None or volumes is None:
        logger.error("Failed to list Modal resources. Cannot proceed.")
        return 1

    _display_resources(apps, volumes)
    if not apps and not volumes:
        logger.info("Nothing to nuke.")
        return 0

    if args.dry_run:
        logger.info("Dry run -- no changes made.")
        return 0

    if not _confirm_nuke(args.force):
        logger.info("Aborted.")
        return 1

    failures = _execute_nuke(apps, volumes, environment)

    if failures:
        logger.error(f"Nuke finished with {failures} failure(s).")
        return 1
    logger.info("Nuke complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
