"""Verify that a workspace's backup service is at (or above) the minimum required version.

Runs as part of the per-workspace backups route: for an online,
verification-enabled workspace, one ``mngr exec`` runs the stdlib-only check
script (see ``backup_workspace_scripts``) which compares the installed
``libs/host_backup`` content against the *minimum required* ``minds-v*`` tag
(fetching tags from the ``official`` remote only when the tag is missing
locally), reports the supervisord state of the ``host-backup`` program, and
returns the workspace's ``restic.env`` (sha256 + content).

minds then classifies the result into problems. At-or-above the minimum is
fine: content matching the minimum tag, the minimum tag being an ancestor of
the workspace HEAD (which also silently accepts user edits on top), or an
installed ``backup-update:`` identity at or above the minimum all produce no
warning. A workspace with a working, hand-configured ``restic.env`` and no
minds-side canonical env is *adopted*: the env is pulled into the canonical
store during the check so status and management just start working.

Offline workspaces report ``OFFLINE`` (no badge); workspaces with verification
disabled report ``DISABLED`` and are never exec'd into. Cross-workspace
parallelism is the frontend's job -- there is deliberately no batch entry
point here.
"""

import base64
import os
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import WorkspacePaths
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
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# One exec runs the whole check; sized to cover a first-encounter `git fetch
# official --tags` on a slow network (the script gives the fetch 300s) on top
# of the (fast, local) git diff, so the script's structured "fetch failed"
# detail always beats the exec timeout.
_CHECK_EXEC_TIMEOUT_SECONDS: Final[float] = 360.0

# The minimum required backup-service version. Workspaces at or above it are
# never flagged; the update action still converges to the tag matching the
# running minds release. Bumped manually only when a newer backup service is
# actually required -- this deliberately avoids re-flagging every workspace on
# every release. Overridable via MINDS_MINIMUM_BACKUP_TAG for dev/testing.
MINIMUM_BACKUP_SERVICE_TAG: Final[str] = "minds-v0.3.4"
MINIMUM_BACKUP_TAG_ENV_VAR: Final[str] = "MINDS_MINIMUM_BACKUP_TAG"

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
    minimum_version: str | None = Field(
        default=None, description="The minimum required minds-v* tag the check compared against"
    )
    detail: str = Field(default="", description="Human-readable extra detail (e.g. why unverifiable)")


def minimum_backup_tag() -> str:
    """The minimum required backup-service tag (env-overridable for dev/testing)."""
    return os.environ.get(MINIMUM_BACKUP_TAG_ENV_VAR) or MINIMUM_BACKUP_SERVICE_TAG


def update_target_backup_tag() -> str:
    """The minds-v* tag the update action converges to (the running release).

    Display-only on the minds side: the update script itself re-resolves the
    version (falling back to the highest available tag for dev builds).
    """
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
        minimum_version=str(payload.get("target_tag") or "") or None,
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
        BACKUP_CHECK_SCRIPT, ("--minimum-tag", minimum_backup_tag(), "--agent-id", str(agent_id))
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
            minimum_version=minimum_backup_tag(),
            detail=f"check could not run: {detail}",
        )
    payload = extract_marker_json(result.stdout, CHECK_RESULT_MARKER)
    if payload is None:
        return BackupServiceCheck(
            state=BackupServiceCheckState.PROBLEMS,
            problems=(BackupServiceProblem.UNVERIFIABLE,),
            minimum_version=minimum_backup_tag(),
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
