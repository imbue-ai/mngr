"""Helpers to trigger the changelog-consolidation schedule on demand.

The nightly consolidation runs at midnight Pacific. ``scripts/release.py``
uses these helpers to detect unconsolidated entries in ``changelog/`` and,
with the user's consent, kick off the same Modal trigger immediately so
the resulting PR can be merged before cutting the release.

``setup_changelog_agent.sh`` reuses ``disable_plugin_args`` so the deploy
script and the on-demand trigger stay in sync about which plugins must
be disabled around the ``mngr schedule`` invocations.
"""

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

TRIGGER_NAME: Final[str] = "changelog-consolidation"
MNGR_ROOT_NAME: Final[str] = "mngr-changelog-schedule"
# Plugins the schedule trigger needs to function; everything else is
# disabled to avoid loading repo-specific plugins that would error on
# import in this isolated mngr config namespace.
_ENABLED_PLUGINS: Final[frozenset[str]] = frozenset({"schedule", "modal", "headless_claude", "claude", "file"})


def pending_changelog_entries(repo_root: Path) -> list[Path]:
    """Return changelog entry files awaiting consolidation.

    Excludes ``.gitkeep`` and non-``.md`` files, matching the filter
    ``consolidate_changelog.py`` uses, so this is an exact predicate for
    "would the consolidator have something to do".
    """
    changelog_dir = repo_root / "changelog"
    if not changelog_dir.is_dir():
        return []
    entries: list[Path] = []
    for path in sorted(changelog_dir.iterdir()):
        if path.name == ".gitkeep" or not path.name.endswith(".md"):
            continue
        entries.append(path)
    return entries


def disable_plugin_args() -> list[str]:
    """Compute ``--disable-plugin <name>`` args for ``mngr schedule`` invocations.

    The mngr CLI auto-loads every plugin registered in the ``mngr``
    entry-point group. Many of this repo's plugins reference code that
    isn't importable from the isolated config namespace the consolidation
    schedule uses, so we disable everything except the minimum set the
    schedule needs.
    """
    installed = {ep.name for ep in importlib.metadata.entry_points(group="mngr")}
    to_disable = sorted(installed - _ENABLED_PLUGINS)
    args: list[str] = []
    for name in to_disable:
        args.extend(["--disable-plugin", name])
    return args


def _trigger_env() -> dict[str, str]:
    """Return the environment for invoking ``mngr schedule run``.

    Mirrors ``setup_changelog_agent.sh``: unsets the caller's mngr host
    directory and config prefix and points the CLI at the isolated
    ``mngr-changelog-schedule`` config namespace where the trigger lives.
    """
    env = os.environ.copy()
    env.pop("MNGR_HOST_DIR", None)
    env.pop("MNGR_PREFIX", None)
    env["MNGR_ROOT_NAME"] = MNGR_ROOT_NAME
    return env


def run_trigger(provider: str = "modal") -> int:
    """Invoke ``mngr schedule run`` for the consolidation trigger.

    Streams stdout/stderr live so the human watching the release sees the
    agent's progress and final JSON outcome (containing the PR URL).
    Returns the subprocess exit code.
    """
    cmd = [
        "uv",
        "run",
        "mngr",
        "schedule",
        "run",
        TRIGGER_NAME,
        "--provider",
        provider,
        *disable_plugin_args(),
    ]
    result = subprocess.run(cmd, env=_trigger_env(), check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-disable-plugin-args",
        action="store_true",
        help="Print the --disable-plugin args (space-separated) and exit. "
        "Used by setup_changelog_agent.sh to stay in sync with the on-demand trigger.",
    )
    parser.add_argument(
        "--provider",
        default="modal",
        help="Schedule provider to run on (default: modal).",
    )
    args = parser.parse_args()

    if args.print_disable_plugin_args:
        print(" ".join(disable_plugin_args()))
        return 0

    return run_trigger(provider=args.provider)


if __name__ == "__main__":
    sys.exit(main())
