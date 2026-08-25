"""Low-level identity mutation for a host's on-disk state.

An agent's id lives in two places in host state -- the name of its state
directory (``agents/<agent_id>/``) and the ``"id"`` field of the
``data.json`` inside it -- and a host's own id lives in the ``host_id``
field of the host-level ``data.json``. These functions rewrite exactly that
state -- plus, for providers with an external agent store, the externally
persisted agent-data copy, which is re-keyed to follow the state dir. No
other provider-side identity (container labels, VM names) is touched, no
semantics beyond "this state now carries a different id" are implied, and
there is no CLI.
Callers are the higher-level flows that need to re-identify state they have
placed on a host (e.g. restoring a backup onto a fresh host, or cloning one
agent's state into a new identity).

Both functions are convergent: re-running after a partial failure completes
the mutation rather than corrupting it.
"""

import json
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import HostError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


class IdMutationError(HostError):
    """Raised when a host-state identity mutation cannot be performed safely."""


def mutate_agent_id(
    host: OnlineHostInterface, old_agent_id: AgentId, new_agent_id: AgentId, config: MngrConfig
) -> None:
    """Rewrite one stopped agent's identity in the host's on-disk state.

    Renames the agent's state directory and rewrites the ``"id"`` field of its
    ``data.json`` (all other fields, known or unknown, are preserved
    verbatim); any externally persisted agent-data copy is re-keyed the same
    way (saved under the new id, removed under the old). Refuses when the
    agent's tmux session is still running, when the target id already exists
    on the host as a different agent, when the source agent does not exist,
    or when the state dir's ``data.json`` records an id belonging to some
    other agent (matching neither end of the mutation).
    Historical event lines that embed the old id are left as written (they are
    append-only history).

    ``config`` supplies the session-naming rule for the running-session guard
    on hosts whose certified data records no tmux session prefix.
    """
    if new_agent_id == old_agent_id:
        raise IdMutationError(f"Cannot mutate agent id {old_agent_id} onto itself")
    old_dir = get_agent_state_dir_path(host.host_dir, old_agent_id)
    new_dir = get_agent_state_dir_path(host.host_dir, new_agent_id)
    is_old_present = host.path_exists(old_dir)
    is_new_present = host.path_exists(new_dir)

    if is_old_present and is_new_present:
        raise IdMutationError(
            f"Cannot mutate agent id {old_agent_id} -> {new_agent_id}: an agent with the target id "
            f"already exists on host {host.id}"
        )
    elif not is_old_present and not is_new_present:
        raise IdMutationError(f"Cannot mutate agent id {old_agent_id} -> {new_agent_id}: no such agent state dir")
    elif is_old_present:
        # Refuse while the agent's session is up: a running process holds
        # MNGR_AGENT_ID and its state-dir path in its environment, and moving
        # the dir under it would corrupt the running agent.
        agent_data = _read_agent_data(host, old_dir / "data.json", old_agent_id)
        source_id = agent_data.get("id")
        if source_id != str(old_agent_id):
            # Validate before the mv: renaming a dir whose data.json belongs
            # to some other agent would leave state no retry could converge.
            raise IdMutationError(
                f"Agent state dir {old_dir} belongs to agent {source_id!r}, not {old_agent_id}; refusing to rewrite it"
            )
        _raise_if_agent_session_is_running(host, agent_data, old_agent_id, config)
        # The directory move is the visible switch; the data.json rewrite below
        # is the commit point that makes the state self-consistent again.
        move_result = host.execute_stateful_command(f"mv {shlex.quote(str(old_dir))} {shlex.quote(str(new_dir))}")
        if not move_result.success:
            raise IdMutationError(
                f"Could not move agent state dir {old_dir} -> {new_dir}: {move_result.stderr.strip()}"
            )
    else:
        # The move already happened (a prior attempt was interrupted after the
        # rename); fall through and converge the data.json rewrite.
        pass

    stored_data = _read_agent_data(host, new_dir / "data.json", new_agent_id)
    stored_id = stored_data.get("id")
    if stored_id not in (str(old_agent_id), str(new_agent_id)):
        raise IdMutationError(
            f"Agent state dir {new_dir} belongs to agent {stored_id!r}, not {old_agent_id}; refusing to rewrite it"
        )
    if stored_id != str(new_agent_id):
        stored_data["id"] = str(new_agent_id)
        host.write_file(
            new_dir / "data.json", json.dumps(stored_data, indent=2).encode("utf-8"), mode=None, is_atomic=True
        )
    # Re-key the externally persisted copy (a no-op for providers without an
    # external agent store): persist under the new id first, then drop the old
    # copy so an interruption leaves both rather than neither.
    host.save_agent_data(new_agent_id, stored_data)
    host.remove_agent_data(old_agent_id)
    logger.debug("Mutated agent id {} -> {} on host {}", old_agent_id, new_agent_id, host.id)


def mutate_host_id(host: OnlineHostInterface, new_host_id: HostId) -> None:
    """Re-stamp the ``host_id`` field of the host-level ``data.json``.

    Used to make restored host state agree with the identity of the host it
    now lives on (e.g. after a backup's ``data.json`` -- carrying the old
    host's id -- lands on a fresh host). All other fields are preserved
    verbatim; a ``data.json`` already carrying the target id is a no-op.
    """
    data_path = host.host_dir / "data.json"
    try:
        raw_content = host.read_text_file(data_path)
    except FileNotFoundError as e:
        raise IdMutationError(f"Host {host.id} has no data.json to re-stamp at {data_path}") from e
    try:
        host_data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise IdMutationError(f"Host data.json at {data_path} is not valid JSON: {e}") from e
    if not isinstance(host_data, dict):
        raise IdMutationError(f"Host data.json at {data_path} does not hold an object")
    old_host_id = host_data.get("host_id")
    if old_host_id == str(new_host_id):
        return
    host_data["host_id"] = str(new_host_id)
    host.write_file(data_path, json.dumps(host_data, indent=2).encode("utf-8"), mode=None, is_atomic=True)
    logger.debug("Re-stamped host data.json {} -> {} at {}", old_host_id, new_host_id, data_path)


def _read_agent_data(host: OnlineHostInterface, data_path: Path, agent_id: AgentId) -> dict[str, Any]:
    try:
        raw_content = host.read_text_file(data_path)
    except FileNotFoundError as e:
        raise IdMutationError(f"Agent {agent_id} has no data.json at {data_path}") from e
    try:
        agent_data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise IdMutationError(f"Agent data.json at {data_path} is not valid JSON: {e}") from e
    if not isinstance(agent_data, dict):
        raise IdMutationError(f"Agent data.json at {data_path} does not hold an object")
    return agent_data


def _raise_if_agent_session_is_running(
    host: OnlineHostInterface, agent_data: dict[str, Any], agent_id: AgentId, config: MngrConfig
) -> None:
    """Raise when the agent's tmux session exists (the agent is not stopped).

    A missing tmux binary or tmux server counts as not running -- ``tmux
    has-session`` fails for both, and with no server there can be no session.
    """
    agent_name = agent_data.get("name")
    if not isinstance(agent_name, str) or not agent_name:
        # Without a name there is no derivable session to check; state this
        # loudly rather than guessing (a nameless data.json is corrupt anyway).
        raise IdMutationError(f"Agent {agent_id} has no name in its data.json; cannot verify it is stopped")
    session_name = _expected_agent_session_name(host, config, agent_name)
    # ``=`` forces an exact session-name match (no prefix matching).
    probe = host.execute_idempotent_command(f"tmux has-session -t ={shlex.quote(session_name)} 2>/dev/null")
    if probe.success:
        raise IdMutationError(
            f"Agent {agent_id} ({agent_name}) still has a running tmux session {session_name!r}; "
            "stop it before mutating its id"
        )


def _expected_agent_session_name(host: OnlineHostInterface, config: MngrConfig, agent_name: str) -> str:
    """The tmux session name the agent's session would carry on this host.

    The certified data's recorded prefix wins when present (it captures the
    prefix the host's sessions were actually created with, even by another
    mngr context); hosts that record none (local, vps, imbue_cloud) use the
    calling context's configured prefix + name rule.
    """
    prefix = host.get_certified_data().tmux_session_prefix
    if prefix:
        return f"{prefix}{agent_name}"
    return config.agent_session_name(agent_name)
