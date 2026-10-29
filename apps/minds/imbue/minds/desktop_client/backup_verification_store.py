"""Per-workspace "backup verification enabled" flag, kept in the workspace's own backup config.

Verification (the exec-based check that a workspace's backup service matches what Imbue Studio
would install today) is on by default for every workspace. A user who genuinely doesn't want
backups can disable it per workspace; while disabled, no full checks run against that
workspace and no warning badge is shown (the laptop-side snapshot status keeps working
regardless).

The flag is a fact about one workspace every computer should agree on (spec section 7.4), so
it lives as the ``verification_disabled`` key in the workspace's ``data/system/backup.toml``,
written through the ``backup_verification_flag`` workspace program. Each install serves its
cached copy (the per-workspace facts cache) while the workspace is stopped; a toggle made then
is queued and written through by the sync scheduler once the workspace runs again.
"""

from typing import Final

from loguru import logger

from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.in_workspace_mngr import inner_stdout_from_exec_result
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_command
from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.desktop_client.workspace_update_state import is_workspace_readable
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.workspace_programs import backup_verification_flag
from imbue.minds.workspace_programs.backup_protocol import VERIFICATION_FLAG_RESULT_MARKER
from imbue.minds.workspace_programs.backup_verification_flag import SET_DISABLED_VALUE
from imbue.minds.workspace_programs.backup_verification_flag import SET_ENABLED_VALUE
from imbue.minds.workspace_programs.backup_verification_flag import VERIFICATION_DISABLED_KEY
from imbue.mngr.primitives import AgentId

_FLAG_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0


def is_backup_verification_enabled(facts_cache: WorkspaceFactsCache, agent_id: AgentId) -> bool:
    """Whether backup verification is enabled for this workspace, as last known here (default True)."""
    return facts_cache.is_backup_verification_enabled(agent_id)


def set_backup_verification_enabled(
    facts_cache: WorkspaceFactsCache,
    agent_id: AgentId,
    is_enabled: bool,
    *,
    mngr_caller: MngrCaller | None,
    backend_resolver: BackendResolverInterface,
) -> None:
    """Record the flag and write it into the workspace's ``backup.toml`` when it can be reached now.

    The cache answers every reader at once; a write-through that cannot land (a stopped
    workspace, a failed exec) stays queued and the sync scheduler retries it each pass.
    """
    facts_cache.set_backup_verification_enabled(agent_id, is_enabled)
    if mngr_caller is not None:
        _write_flag_through(facts_cache, agent_id, mngr_caller=mngr_caller, backend_resolver=backend_resolver)


def flush_pending_backup_verification_writes(
    facts_cache: WorkspaceFactsCache,
    *,
    mngr_caller: MngrCaller,
    backend_resolver: BackendResolverInterface,
) -> None:
    """Write every queued verification flag into its workspace, where the workspace is running."""
    host_state_by_agent = backend_resolver.list_active_workspace_host_states()
    for agent_id in facts_cache.list_agents_with_dirty_backup_verification():
        # An unknown host state counts as reachable, per the resolver's contract; a failed
        # exec just stays queued for the next pass.
        if agent_id not in host_state_by_agent or not is_workspace_readable(host_state_by_agent[agent_id]):
            continue
        _write_flag_through(facts_cache, agent_id, mngr_caller=mngr_caller, backend_resolver=backend_resolver)


def _write_flag_through(
    facts_cache: WorkspaceFactsCache,
    agent_id: AgentId,
    *,
    mngr_caller: MngrCaller,
    backend_resolver: BackendResolverInterface,
) -> None:
    record = facts_cache.read(agent_id)
    if not record.is_backup_verification_dirty:
        return
    requested = SET_DISABLED_VALUE if record.is_backup_verification_disabled else SET_ENABLED_VALUE
    command = build_workspace_program_command(backup_verification_flag, ("--set", requested))
    result = mngr_caller.call(
        ["exec", build_agent_address(agent_id, backend_resolver), command, "--no-start", "--format", "jsonl"],
        timeout=_FLAG_EXEC_TIMEOUT_SECONDS,
    )
    payload = extract_marker_json(inner_stdout_from_exec_result(result.stdout), VERIFICATION_FLAG_RESULT_MARKER)
    if payload is None or payload.get("is_set_applied") is not True:
        logger.debug("Deferred the backup-verification flag write for {}: {}", agent_id, result.stderr.strip()[-300:])
        return
    facts_cache.adopt_backup_verification(
        agent_id,
        is_disabled=payload.get(VERIFICATION_DISABLED_KEY) is True,
        is_queued_write_applied=True,
    )
