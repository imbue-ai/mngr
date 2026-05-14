"""Helpers to trigger the changelog-consolidation schedule on demand.

The nightly consolidation runs at midnight Pacific. ``scripts/release.py``
uses this module to detect unconsolidated entries in ``changelog/`` and,
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
from collections.abc import Callable
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


def _format_pending_list(entries: list[Path], repo_root: Path) -> str:
    lines = [f"  - {entry.relative_to(repo_root)}" for entry in entries]
    return "\n".join(lines)


def gate_release_on_pending_entries(
    repo_root: Path,
    provider: str = "modal",
    dry_run: bool = False,
    input_fn: Callable[[str], str] = input,
    run_trigger_fn: Callable[[str], int] = run_trigger,
) -> bool:
    """Block a release until pending changelog entries are consolidated.

    Returns ``True`` if the release may proceed (no pending entries, or
    ``dry_run`` is set), ``False`` if the caller must abort (entries are
    pending; the agent was triggered or the user declined). Either way,
    the caller should re-run ``release.py`` after merging the
    consolidation PR.

    ``dry_run`` swaps the prompt for a warning so ``release.py --dry-run``
    can still preview what would be released; a real release attempt with
    pending entries will hit the prompt.

    ``input_fn`` and ``run_trigger_fn`` are injected so tests can drive
    the gate's branching without monkeypatching the module's globals.
    """
    entries = pending_changelog_entries(repo_root)
    if not entries:
        return True

    if dry_run:
        print()
        print(f"WARNING: {len(entries)} pending changelog entry/entries would block a real release:")
        print(_format_pending_list(entries, repo_root))
        print(f"(use '{TRIGGER_NAME}' to consolidate before cutting the release)")
        print()
        return True

    print()
    print(f"ERROR: cannot release with {len(entries)} pending changelog entry/entries.")
    print()
    print("The following entries in changelog/ haven't been consolidated into")
    print("CHANGELOG.md's [Unreleased] section yet:")
    print(_format_pending_list(entries, repo_root))
    print()
    print("Release notes for this version would not include them. The nightly")
    print(f"consolidation agent ('{TRIGGER_NAME}') normally handles this at midnight")
    print("Pacific. You can trigger it on demand now (runs on Modal, takes a few")
    print("minutes, opens a PR for you to review and merge).")
    print()
    answer = input_fn(f"Trigger the {TRIGGER_NAME} agent now? [y/N] ")
    if answer.strip().lower() != "y":
        print()
        print("Aborted. Wait for the nightly cron or re-run release.py and answer 'y'.")
        return False

    print()
    print(f"Triggering '{TRIGGER_NAME}' on {provider}...")
    exit_code = run_trigger_fn(provider)
    print()
    if exit_code != 0:
        print(f"ERROR: 'mngr schedule run' exited {exit_code}. See output above.")
        return False
    print("Consolidation finished. Review and merge the PR it opened, then re-run")
    print("scripts/release.py.")
    return False


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
