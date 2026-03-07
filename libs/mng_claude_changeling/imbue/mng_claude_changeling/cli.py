"""CLI commands for the changeling plugin.

These are internal supporting service commands registered with the mng CLI
via the ``register_cli_commands`` plugin hook. They are invoked as tmux
window commands (e.g. ``mng changelingevents``) and are not intended for
direct user invocation.

Command names are single words (no hyphens/underscores) because the mng
env var parsing convention (MNG_COMMANDS_<CMD>_<PARAM>) requires it.
"""

from __future__ import annotations

from collections.abc import Sequence

import click


@click.command("changelingevents", hidden=True)
def changelingevents() -> None:
    """Run the changeling event watcher (internal)."""
    from imbue.mng_claude_changeling.resources.event_watcher import main

    main()


@click.command("changelingconversations", hidden=True)
def changelingconversations() -> None:
    """Run the changeling conversation watcher (internal)."""
    from imbue.mng_claude_changeling.resources.conversation_watcher import main

    main()


@click.command("changelingtranscript", hidden=True)
def changelingtranscript() -> None:
    """Run the changeling transcript watcher (internal)."""
    from imbue.mng_claude_changeling.resources.transcript_watcher import main

    main()


@click.command("changelingweb", hidden=True)
def changelingweb() -> None:
    """Run the changeling web server (internal)."""
    from imbue.mng_claude_changeling.resources.web_server import main

    main()


# -- changelingdb: click Group with subcommands --
# Subcommand names are allowed to have hyphens since only top-level
# command names participate in MNG_COMMANDS_* env var parsing.


@click.group("changelingdb", hidden=True)
def changelingdb() -> None:
    """Conversation database operations (internal)."""


@changelingdb.command()
@click.argument("db_path")
@click.argument("conversation_id")
@click.argument("tags")
@click.argument("created_at")
def insert(db_path: str, conversation_id: str, tags: str, created_at: str) -> None:
    """Insert a conversation record into the changeling_conversations table."""
    from imbue.mng_claude_changeling.resources.conversation_db import db_insert

    db_insert(db_path, conversation_id, tags, created_at)


@changelingdb.command("lookup-model")
@click.argument("db_path")
@click.argument("conversation_id")
def lookup_model(db_path: str, conversation_id: str) -> None:
    """Look up the model for a conversation."""
    from imbue.mng_claude_changeling.resources.conversation_db import db_lookup_model

    db_lookup_model(db_path, conversation_id)


@changelingdb.command()
@click.argument("db_path")
def count(db_path: str) -> None:
    """Count conversations in the changeling_conversations table."""
    from imbue.mng_claude_changeling.resources.conversation_db import db_count

    db_count(db_path)


@changelingdb.command("max-rowid")
@click.argument("db_path")
def max_rowid(db_path: str) -> None:
    """Get the maximum rowid from the conversations table."""
    from imbue.mng_claude_changeling.resources.conversation_db import db_max_rowid

    db_max_rowid(db_path)


@changelingdb.command("poll-new")
@click.argument("db_path")
@click.argument("after_rowid")
def poll_new(db_path: str, after_rowid: str) -> None:
    """Poll for a new conversation after the given rowid."""
    from imbue.mng_claude_changeling.resources.conversation_db import db_poll_new

    db_poll_new(db_path, after_rowid)


def get_all_commands() -> Sequence[click.Command]:
    """Return all CLI commands to register with mng."""
    return [
        changelingevents,
        changelingconversations,
        changelingtranscript,
        changelingweb,
        changelingdb,
    ]
