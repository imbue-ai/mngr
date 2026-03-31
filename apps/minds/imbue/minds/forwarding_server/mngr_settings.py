"""Configure .mngr/settings.toml for mind repositories.

When a mind is created, the agent should only see its own agents in
``mngr list`` and should not receive event notifications about its own
state changes.  This module writes the necessary default CLI filters
into the project-level mngr config file.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import tomlkit
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.minds.errors import GitOperationError
from imbue.minds.forwarding_server.vendor_mngr import ensure_git_identity
from imbue.minds.forwarding_server.vendor_mngr import run_git
from imbue.minds.primitives import AgentName
from imbue.mngr.primitives import AgentId

MNGR_SETTINGS_DIR_NAME: Final[str] = ".mngr"
MNGR_SETTINGS_FILE_NAME: Final[str] = "settings.toml"

_AGENT_STATES_SOURCE: Final[str] = "mngr/agent_states"


def _build_list_exclude_filter(mind_name: AgentName) -> str:
    """Build a CEL expression that excludes agents not belonging to this mind."""
    return '!has(labels.mind) || labels.mind != "{}" || name == "{}"'.format(mind_name, mind_name)


def _build_events_self_include_filter(agent_id: AgentId) -> str:
    """Build a CEL include filter that excludes this agent's own state change events."""
    return 'source != "{}" || agent_id != "{}"'.format(_AGENT_STATES_SOURCE, agent_id)


def configure_mngr_settings(
    repo_dir: Path,
    mind_name: AgentName,
    agent_id: AgentId,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Configure .mngr/settings.toml so the mind filters its own view.

    Creates or updates the project-level mngr config to:
    1. Add an exclude filter to ``[commands.list]`` so ``mngr list`` only
       shows agents labelled with this mind's name.
    2. Add an include filter to ``[commands.events]`` so ``mngr events``
       excludes this agent's own ``mngr/agent_states`` events.

    Existing settings are preserved; new entries are merged in.
    The resulting file is staged and committed on the current branch.
    """
    with log_span("Configuring .mngr/settings.toml for mind {}", mind_name):
        settings_dir = repo_dir / MNGR_SETTINGS_DIR_NAME
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / MNGR_SETTINGS_FILE_NAME

        # Read existing settings (or start fresh)
        doc = tomlkit.document()
        if settings_path.exists():
            doc = tomlkit.parse(settings_path.read_text())

        _add_list_exclude_filter(doc, mind_name)
        _add_events_self_include_filter(doc, agent_id)

        settings_path.write_text(tomlkit.dumps(doc))

        _commit_mngr_settings(repo_dir, on_output)


def _add_list_exclude_filter(doc: tomlkit.TOMLDocument, mind_name: AgentName) -> None:
    """Add an exclude filter to [commands.list] in the TOML document."""
    commands = _ensure_super_table(doc, "commands")
    list_table = _ensure_table(commands, "list")

    exclude_filter = _build_list_exclude_filter(mind_name)
    existing_excludes: list[str] = list(list_table.get("exclude", []))

    if exclude_filter not in existing_excludes:
        existing_excludes.append(exclude_filter)

    list_table["exclude"] = existing_excludes


def _add_events_self_include_filter(doc: tomlkit.TOMLDocument, agent_id: AgentId) -> None:
    """Add an include filter to [commands.events] in the TOML document."""
    commands = _ensure_super_table(doc, "commands")
    events_table = _ensure_table(commands, "events")

    new_filter = _build_events_self_include_filter(agent_id)
    existing_includes: list[str] = list(events_table.get("include", []))

    if new_filter not in existing_includes:
        existing_includes.append(new_filter)

    events_table["include"] = existing_includes


def _ensure_super_table(doc: tomlkit.TOMLDocument, key: str) -> Any:
    """Return the named super-table, creating it if absent."""
    if key not in doc:
        doc.add(key, tomlkit.table(is_super_table=True))
    return doc[key]


def _ensure_table(parent: Any, key: str) -> Any:
    """Return a sub-table under *parent*, creating it if absent.

    Uses dict-style assignment rather than ``.add()`` because
    *parent* may be an ``OutOfOrderTableProxy`` (returned by tomlkit
    when sub-tables are interleaved with other sections), which does
    not expose an ``add`` method.
    """
    if key not in parent:
        parent[key] = tomlkit.table()
    return parent[key]


def _commit_mngr_settings(
    repo_dir: Path,
    on_output: Callable[[str, bool], None] | None = None,
) -> None:
    """Stage and commit .mngr/settings.toml."""
    ensure_git_identity(repo_dir)

    settings_rel_path = "{}/{}".format(MNGR_SETTINGS_DIR_NAME, MNGR_SETTINGS_FILE_NAME)

    run_git(
        ["add", settings_rel_path],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to stage {}".format(settings_rel_path),
        error_class=GitOperationError,
    )

    # Check if there are staged changes before committing
    diff_output = run_git(
        ["diff", "--cached", "--name-only"],
        cwd=repo_dir,
        error_message="Failed to check staged changes",
        error_class=GitOperationError,
    ).strip()

    if not diff_output:
        logger.debug("No changes to {}, skipping commit", settings_rel_path)
        return

    run_git(
        ["commit", "-m", "Configure mngr settings for mind"],
        cwd=repo_dir,
        on_output=on_output,
        error_message="Failed to commit {}".format(settings_rel_path),
        error_class=GitOperationError,
    )
