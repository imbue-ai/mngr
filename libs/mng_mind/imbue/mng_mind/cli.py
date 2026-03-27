"""CLI commands for the mng_mind plugin.

These are internal supporting service commands registered with the mng CLI
via the ``register_cli_commands`` plugin hook. They are invoked as tmux
window commands (e.g. ``mng mindevents``) and are not intended for
direct user invocation.

Command names are single words (no hyphens/underscores) because the mng
env var parsing convention (MNG_COMMANDS_<CMD>_<PARAM>) requires it.
"""

from __future__ import annotations

from collections.abc import Sequence

import click


@click.command("mindevents", hidden=True)
def mindevents() -> None:
    """Run the mind event watcher (internal)."""
    from imbue.mng_mind.event_watcher import main

    main()


def get_all_commands() -> Sequence[click.Command]:
    """Return all CLI commands to register with mng."""
    return [
        mindevents,
    ]
