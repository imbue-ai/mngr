"""CLI commands for the mngr_mind plugin.

These are internal supporting service commands registered with the mngr CLI
via the ``register_cli_commands`` plugin hook. They are invoked as tmux
window commands (e.g. ``mngr mindevents``) and are not intended for
direct user invocation.

Command names are single words (no hyphens/underscores) because the mngr
env var parsing convention (MNGR_COMMANDS_<CMD>_<PARAM>) requires it.
"""

from __future__ import annotations

from collections.abc import Sequence

import click


@click.command("mindevents", hidden=True)
def mindevents() -> None:
    """Run the mind event watcher (internal)."""
    from imbue.mngr_mind.event_watcher import main

    main()


def get_all_commands() -> Sequence[click.Command]:
    """Return all CLI commands to register with mngr."""
    return [
        mindevents,
    ]
