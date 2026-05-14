"""Shared plugin-disable args for the changelog-consolidation schedule.

``setup_changelog_agent.sh`` (which deploys the trigger) and
``scripts/release.py`` (which prints an on-demand invocation when the
pre-release gate fires) both need the same ``--disable-plugin <name>``
list passed to ``mngr schedule …`` calls in the isolated
``mngr-changelog-schedule`` config namespace. This module is the single
source of truth for that list and for the trigger / namespace identifiers.
"""

import argparse
import importlib.metadata
from typing import Final

TRIGGER_NAME: Final[str] = "changelog-consolidation"
MNGR_ROOT_NAME: Final[str] = "mngr-changelog-schedule"
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
        "Used by setup_changelog_agent.sh and release.py so they stay in sync.",
    )
    args = parser.parse_args()
    if args.print_disable_plugin_args:
        print(" ".join(disable_plugin_args()))
        return
    # parser.error() prints usage to stderr and calls sys.exit(2); it does not return.
    parser.error("no action specified; pass --print-disable-plugin-args")


if __name__ == "__main__":
    main()
