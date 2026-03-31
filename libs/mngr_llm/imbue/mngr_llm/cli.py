"""CLI commands for the llm plugin.

These are internal supporting service commands registered with the mngr CLI
via the ``register_cli_commands`` plugin hook. They are invoked as tmux
window commands (e.g. ``mngr llmconversations``) and are not intended for
direct user invocation.

Command names are single words (no hyphens/underscores) because the mngr
env var parsing convention (MNGR_COMMANDS_<CMD>_<PARAM>) requires it.
"""

from __future__ import annotations

from collections.abc import Sequence

import click

from imbue.mngr_llm.resources.conversation_db import last_response_id as _last_response_id


@click.command("llmconversations", hidden=True)
def llmconversations() -> None:
    """Run the llm conversation watcher (internal)."""
    from imbue.mngr_llm.resources.conversation_watcher import main

    main()


@click.command("llmweb", hidden=True)
def llmweb() -> None:
    """Run the llm-webchat based web server (internal)."""
    from imbue.mngr_llm.resources.webchat_server import main

    main()


# -- llmdb: click Group with subcommands --
# Subcommand names are allowed to have hyphens since only top-level
# command names participate in MNGR_COMMANDS_* env var parsing.


@click.group("llmdb", hidden=True)
def llmdb() -> None:
    """Conversation database operations (internal)."""


@llmdb.command()
@click.argument("db_path")
@click.argument("conversation_id")
@click.argument("tags")
@click.argument("created_at")
def insert(db_path: str, conversation_id: str, tags: str, created_at: str) -> None:
    """Insert a conversation record into the mind_conversations table."""
    from imbue.mngr_llm.resources.conversation_db import insert

    insert(db_path, conversation_id, tags, created_at)


@llmdb.command("lookup-model")
@click.argument("db_path")
@click.argument("conversation_id")
def lookup_model(db_path: str, conversation_id: str) -> None:
    """Look up the model for a conversation."""
    from imbue.mngr_llm.resources.conversation_db import lookup_model

    lookup_model(db_path, conversation_id)


@llmdb.command()
@click.argument("db_path")
def count(db_path: str) -> None:
    """Count conversations in the mind_conversations table."""
    from imbue.mngr_llm.resources.conversation_db import count

    count(db_path)


@llmdb.command("max-rowid")
@click.argument("db_path")
def max_rowid(db_path: str) -> None:
    """Get the maximum rowid from the conversations table."""
    from imbue.mngr_llm.resources.conversation_db import max_rowid

    max_rowid(db_path)


@llmdb.command("lookup-by-name")
@click.argument("db_path")
@click.argument("name")
def lookup_by_name(db_path: str, name: str) -> None:
    """Look up a conversation ID by its name tag."""
    from imbue.mngr_llm.resources.conversation_db import lookup_by_name

    lookup_by_name(db_path, name)


@llmdb.command("poll-new")
@click.argument("db_path")
@click.argument("after_rowid")
def poll_new(db_path: str, after_rowid: str) -> None:
    """Poll for a new conversation after the given rowid."""
    from imbue.mngr_llm.resources.conversation_db import poll_new

    poll_new(db_path, after_rowid)


@llmdb.command("last-response-id")
@click.argument("db_path")
@click.argument("conversation_id")
def last_response_id_cmd(db_path: str, conversation_id: str) -> None:
    """Get the most recent response ID for a conversation."""
    _last_response_id(db_path, conversation_id)


def get_all_commands() -> Sequence[click.Command]:
    """Return all CLI commands to register with mngr."""
    return [
        llmconversations,
        llmweb,
        llmdb,
    ]
