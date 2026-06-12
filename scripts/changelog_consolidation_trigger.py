"""Shared identifiers + plugin-disable args for the changelog-consolidation schedule.

``changelog_deploy.sh`` (which deploys the trigger) and
``scripts/release.py`` (which prints an on-demand invocation when the
pre-release gate fires) need to agree on three things: the
``--disable-plugin <name>`` list passed to ``mngr schedule …`` calls,
the schedule's deployed provider, and the trigger / namespace
identifiers. This module is the source of truth.

Python callers (``release.py``) import the constants and helpers
directly. The shell script reads them through this module's CLI:
``--print-disable-plugin-args`` for the disable list and
``--print-provider`` for the provider. ``TRIGGER_NAME`` and
``MNGR_ROOT_NAME`` are still duplicated as bash literals in
``changelog_deploy.sh`` because that script needs them before any
``uv run python`` invocation; those two literals must be kept in sync
with the constants here by hand.
"""

import argparse
import importlib.metadata
from typing import Final

TRIGGER_NAME: Final[str] = "changelog-consolidation"
MNGR_ROOT_NAME: Final[str] = "mngr-changelog-schedule"
# The schedule is deployed against this provider; release.py's printed
# on-demand command must reference the same value so the command points
# at the deployment that actually exists. Editing in-source is fine --
# (re)deploys are rare and the file edit is the deliberate trigger.
PROVIDER: Final[str] = "modal"
# Plugins the schedule trigger needs to function; everything else is
# disabled to avoid loading repo-specific plugins that would error on
# import in this isolated mngr config namespace.
_ENABLED_PLUGINS: Final[frozenset[str]] = frozenset({"schedule", "modal", "headless_claude", "claude", "file"})


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-disable-plugin-args",
        action="store_true",
        help="Print the --disable-plugin args (space-separated) and exit. "
        "Used by changelog_deploy.sh and release.py so they stay in sync.",
    )
    parser.add_argument(
        "--print-provider",
        action="store_true",
        help="Print the deployed provider name and exit. Used by changelog_deploy.sh.",
    )
    args = parser.parse_args()
    if args.print_disable_plugin_args:
        print(" ".join(disable_plugin_args()))
        return
    if args.print_provider:
        print(PROVIDER)
        return
    # parser.error() prints usage to stderr and calls sys.exit(2); it does not return.
    parser.error("no action specified; pass --print-disable-plugin-args or --print-provider")


if __name__ == "__main__":
    main()
