"""Verify that a workspace's backup service matches what minds would install today.

Runs as an expanded part of the backup status check: for each online,
verification-enabled workspace, one ``mngr exec`` runs the stdlib-only check
script (see ``backup_workspace_scripts``) which compares the installed
``libs/host_backup`` content against the ``minds-v*`` tag matching this app's
version (fetching tags from ``upstream`` only when the tag is missing
locally), reports the supervisord state of the ``host-backup`` program, and
returns the workspace's ``restic.env`` (sha256 + content).

minds then classifies the result into problems. "Newer is fine": when the
target tag is an ancestor of the workspace HEAD and the content differs, the
code is assumed newer (or deliberately edited) and is NOT flagged. A workspace
with a working, hand-configured ``restic.env`` and no minds-side canonical env
is *adopted*: the env is pulled into the canonical store during the check so
status and management just start working.

Offline workspaces report ``OFFLINE`` (no badge); workspaces with verification
disabled report ``DISABLED`` and are never exec'd into.
"""

import base64
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import wait
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import env_content_sha256
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_provisioning import run_mngr_exec_on_agent
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_CHECK_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import CHECK_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# One exec runs the whole check; sized to cover a first-encounter `git fetch
# upstream --tags` on a slow network on top of the (fast, local) git diff.
_CHECK_EXEC_TIMEOUT_SECONDS: Final[float] = 180.0
# Wall-clock budget for the whole batch; a workspace whose check hasn't
# finished reports UNKNOWN (no badge) and completes on a later batch.
_CHECK_BATCH_TIMEOUT_SECONDS: Final[float] = 60.0
_MAX_CHECK_WORKERS: Final[int] = 4

_REQUIRED_ADOPTION_KEYS: Final[tuple[str, ...]] = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")


class BackupServiceProblem(UpperCaseStrEnum):
    """One detected backup-service problem; any of these earns the warning badge."""

    NOT_CONFIGURED = auto()
    CODE_OUTDATED = auto()
    ENV_MISSING = auto()
    ENV_MISMATCH = auto()
    SERVICE_NOT_RUNNING = auto()
    UNVERIFIABLE = auto()


class BackupServiceCheckState(UpperCaseStrEnum):
    """Overall verdict for a workspace's backup-service check."""

    # Everything matches; no badge.
    OK = auto()
    # One or more problems detected; the badge is shown.
    PROBLEMS = auto()
    # The workspace is not reachable right now; nothing extra is shown.
    OFFLINE = auto()
    # Verification is disabled for this workspace; no checks run, no badge.
    DISABLED = auto()
    # The check did not complete in time (it retries on a later batch); no badge.
    UNKNOWN = auto()


class BackupServiceCheck(FrozenModel):
    """The classified result of one workspace's backup-service check."""

    state: BackupServiceCheckState = Field(description="Overall verdict")
    problems: tuple[BackupServiceProblem, ...] = Field(
        default=(), description="Detected problems, empty unless state is PROBLEMS"
    )
    installed_version: str | None = Field(
        default=None, description="Installed backup-code version (last backup-update tag, or nearest minds-v* tag)"
    )
    desired_version: str | None = Field(default=None, description="The minds-v* tag the check compared against")
    detail: str = Field(default="", description="Human-readable extra detail (e.g. why unverifiable)")


def desired_backup_tag() -> str:
    """The minds-v* tag this app version expects workspaces to carry."""
    return f"minds-v{resolve_release_id()}"


def is_workspace_online(resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    """Whether the workspace's host is currently RUNNING (reachable for exec)."""
    display_info = resolver.get_agent_display_info(agent_id)
    if display_info is None:
        return False
    host_state = resolver.get_host_state(HostId(display_info.host_id))
    return host_state == HostState.RUNNING


def classify_check_payload(
    payload: dict[str, object],
    *,
    canonical_env: str | None,
) -> tuple[BackupServiceCheck, str | None]:
    """Classify the check script's payload; returns (check, env_to_adopt).

    ``env_to_adopt`` is the workspace env content when minds holds no canonical
    env but the workspace has a complete one (the caller persists it). Pure so
    the classification rules are directly testable.
    """
    problems: list[BackupServiceProblem] = []
    details: list[str] = []

    code_state = str(payload.get("code_state", "unverifiable"))
    if code_state == "outdated":
        problems.append(BackupServiceProblem.CODE_OUTDATED)
    elif code_state in ("matches", "newer"):
        pass
    else:
        problems.append(BackupServiceProblem.UNVERIFIABLE)
        code_detail = str(payload.get("code_detail", ""))
        if code_detail:
            details.append(code_detail)

    service_state = str(payload.get("service_state", "unknown"))
    if service_state != "running":
        problems.append(BackupServiceProblem.SERVICE_NOT_RUNNING)
        service_detail = str(payload.get("service_detail", ""))
        if service_detail:
            details.append(service_detail)

    env_payload = payload.get("env")
    env_info: dict[str, object] = (
        {str(key): value for key, value in env_payload.items()} if isinstance(env_payload, dict) else {}
    )
    is_env_present = bool(env_info.get("present"))
    env_sha = str(env_info.get("sha256", ""))
    env_to_adopt: str | None = None

    if canonical_env is None:
        adoptable = _decode_adoptable_env(env_info) if is_env_present else None
        if adoptable is not None:
            env_to_adopt = adoptable
        else:
            problems.append(BackupServiceProblem.NOT_CONFIGURED)
    elif not is_env_present:
        problems.append(BackupServiceProblem.ENV_MISSING)
    elif env_sha != env_content_sha256(canonical_env):
        problems.append(BackupServiceProblem.ENV_MISMATCH)
    else:
        pass

    state = BackupServiceCheckState.PROBLEMS if problems else BackupServiceCheckState.OK
    check = BackupServiceCheck(
        state=state,
        problems=tuple(problems),
        installed_version=str(payload.get("installed_version") or "") or None,
        desired_version=str(payload.get("target_tag") or "") or None,
        detail="; ".join(details),
    )
    return check, env_to_adopt


def _decode_adoptable_env(env_info: dict[str, object]) -> str | None:
    """Decode the workspace env and return it iff it is complete enough to adopt."""
    content_b64 = env_info.get("content_b64")
    if not isinstance(content_b64, str) or not content_b64:
        return None
    try:
        content = base64.b64decode(content_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    parsed = parse_restic_env(content)
    if all(parsed.get(key) for key in _REQUIRED_ADOPTION_KEYS):
        return content
    return None


def check_backup_service_for_workspace(
    paths: WorkspacePaths,
    agent_id: AgentId,
    *,
    resolver: BackendResolverInterface,
    parent_cg: ConcurrencyGroup | None = None,
) -> BackupServiceCheck:
    """Run the full backup-service check for one workspace (exec + classify + adopt)."""
    if not is_backup_verification_enabled(paths, agent_id):
        return BackupServiceCheck(state=BackupServiceCheckState.DISABLED)
    if not is_workspace_online(resolver, agent_id):
        return BackupServiceCheck(state=BackupServiceCheckState.OFFLINE)

    command_str = build_workspace_script_command(
        BACKUP_CHECK_SCRIPT, ("--minds-version", resolve_release_id(), "--agent-id", str(agent_id))
    )
    result = run_mngr_exec_on_agent(
        agent_id, command_str, parent_cg=parent_cg, timeout_seconds=_CHECK_EXEC_TIMEOUT_SECONDS
    )
    if result.returncode != 0 or result.is_timed_out:
        detail = (result.stderr or result.stdout).strip()[-500:]
        logger.debug("Backup check exec failed for {}: {}", agent_id, detail)
        return BackupServiceCheck(
            state=BackupServiceCheckState.PROBLEMS,
            problems=(BackupServiceProblem.UNVERIFIABLE,),
            desired_version=desired_backup_tag(),
            detail=f"check could not run: {detail}",
        )
    payload = extract_marker_json(result.stdout, CHECK_RESULT_MARKER)
    if payload is None:
        return BackupServiceCheck(
            state=BackupServiceCheckState.PROBLEMS,
            problems=(BackupServiceProblem.UNVERIFIABLE,),
            desired_version=desired_backup_tag(),
            detail="check produced no parseable result",
        )

    canonical_env = read_canonical_env(paths, agent_id)
    check, env_to_adopt = classify_check_payload(payload, canonical_env=canonical_env)
    if env_to_adopt is not None:
        # Adopt an externally-configured env into the canonical store so
        # status and management start working (also covers a second minds
        # install managing the same workspace).
        logger.info("Adopting externally-configured restic.env for workspace {}", agent_id)
        write_canonical_env(paths, agent_id, env_to_adopt)
    return check


def compute_backup_service_checks(
    paths: WorkspacePaths,
    agent_ids: Sequence[AgentId],
    *,
    resolver: BackendResolverInterface,
    parent_cg: ConcurrencyGroup | None = None,
) -> dict[str, BackupServiceCheck]:
    """Check many workspaces in parallel, bounded in wall-clock.

    Mirrors ``backup_status.compute_backup_status_for_workspaces``: any
    workspace whose check hasn't finished within the batch budget reports
    ``UNKNOWN`` (no badge) and simply completes on a later batch; the executor
    is shut down non-blocking so stragglers never stall the route.
    """
    if not agent_ids:
        return {}
    result_by_agent_id: dict[str, BackupServiceCheck] = {}
    worker_count = min(_MAX_CHECK_WORKERS, len(agent_ids))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="backup-check")
    try:
        future_by_agent_id = {
            agent_id: executor.submit(
                check_backup_service_for_workspace, paths, agent_id, resolver=resolver, parent_cg=parent_cg
            )
            for agent_id in agent_ids
        }
        wait(future_by_agent_id.values(), timeout=_CHECK_BATCH_TIMEOUT_SECONDS)
        for agent_id, future in future_by_agent_id.items():
            try:
                result_by_agent_id[str(agent_id)] = future.result(timeout=0)
            except (FuturesTimeoutError, BackupProvisioningError) as e:
                logger.debug("Backup service check for {} incomplete: {}", agent_id, e)
                result_by_agent_id[str(agent_id)] = BackupServiceCheck(state=BackupServiceCheckState.UNKNOWN)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return result_by_agent_id


def compute_backup_health(
    paths: WorkspacePaths,
    agent_ids: Sequence[AgentId],
    *,
    resolver: BackendResolverInterface,
    parent_cg: ConcurrencyGroup | None = None,
) -> tuple[dict[str, "backup_status.BackupStatus"], dict[str, BackupServiceCheck]]:
    """Run the snapshot-status batch and the verification batch concurrently.

    The service checks fan out on their own single-purpose executor while the
    restic snapshot statuses run on the calling thread; both halves bound their
    own wall-clock, so the combined call finishes in roughly
    max(status budget, check budget).
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup-health")
    try:
        checks_future = executor.submit(
            compute_backup_service_checks, paths, agent_ids, resolver=resolver, parent_cg=parent_cg
        )
        status_by_agent_id = backup_status.compute_backup_status_for_workspaces(paths, agent_ids, parent_cg=parent_cg)
        check_by_agent_id = checks_future.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return status_by_agent_id, check_by_agent_id
