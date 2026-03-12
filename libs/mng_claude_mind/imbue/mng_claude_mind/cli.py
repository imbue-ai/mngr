"""CLI commands for the mind plugin.

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
    from imbue.mng_claude_mind.resources.event_watcher import main

    main()


@click.command("mindconversations", hidden=True)
def mindconversations() -> None:
    """Run the mind conversation watcher (internal)."""
    from imbue.mng_claude_mind.resources.conversation_watcher import main

    main()


@click.command("mindtranscript", hidden=True)
def mindtranscript() -> None:
    """Run the mind transcript watcher (internal)."""
    from imbue.mng_claude_mind.resources.transcript_watcher import main

    main()


@click.command("mindweb", hidden=True)
def mindweb() -> None:
    """Run the mind web server (internal)."""
    from imbue.mng_claude_mind.resources.web_server import main

    main()


# -- minddb: click Group with subcommands --
# Subcommand names are allowed to have hyphens since only top-level
# command names participate in MNG_COMMANDS_* env var parsing.


@click.group("minddb", hidden=True)
def minddb() -> None:
    """Conversation database operations (internal)."""


@minddb.command()
@click.argument("db_path")
@click.argument("conversation_id")
@click.argument("tags")
@click.argument("created_at")
def insert(db_path: str, conversation_id: str, tags: str, created_at: str) -> None:
    """Insert a conversation record into the mind_conversations table."""
    from imbue.mng_claude_mind.resources.conversation_db import insert

    insert(db_path, conversation_id, tags, created_at)


@minddb.command("lookup-model")
@click.argument("db_path")
@click.argument("conversation_id")
def lookup_model(db_path: str, conversation_id: str) -> None:
    """Look up the model for a conversation."""
    from imbue.mng_claude_mind.resources.conversation_db import lookup_model

    lookup_model(db_path, conversation_id)


@minddb.command()
@click.argument("db_path")
def count(db_path: str) -> None:
    """Count conversations in the mind_conversations table."""
    from imbue.mng_claude_mind.resources.conversation_db import count

    count(db_path)


@minddb.command("max-rowid")
@click.argument("db_path")
def max_rowid(db_path: str) -> None:
    """Get the maximum rowid from the conversations table."""
    from imbue.mng_claude_mind.resources.conversation_db import max_rowid

    max_rowid(db_path)


@minddb.command("poll-new")
@click.argument("db_path")
@click.argument("after_rowid")
def poll_new(db_path: str, after_rowid: str) -> None:
    """Poll for a new conversation after the given rowid."""
    from imbue.mng_claude_mind.resources.conversation_db import poll_new

    poll_new(db_path, after_rowid)


def get_all_commands() -> Sequence[click.Command]:
    """Return all CLI commands to register with mng."""
    return [
        mindevents,
        mindconversations,
        mindtranscript,
        mindweb,
        minddb,
    ]
