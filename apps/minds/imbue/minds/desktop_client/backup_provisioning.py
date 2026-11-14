"""Configure restic backups for a workspace after its host is created.

This is the reusable "configure backups for host X" operation. It runs
asynchronously after ``mngr create`` returns the canonical host id (the
desktop client schedules it on a detached thread), but nothing here is
creation-specific: the same entry point can be re-applied to any
already-created host later.

The key idea: Imbue Studio initializes the restic repository itself (from the
machine running Imbue Studio) and gives each workspace its own random repository
password -- the repo's single key. Disaster recovery does not need a repo
"master key": the canonical env (and therefore the random password) syncs
inside the account's encrypted workspace record, unlocked by the master
password via the account DEK (see ``dek_store``). Concretely, enabling
backups:

1. resolves the repository URL + backend credentials (``IMBUE_CLOUD``:
   create the workspace's backup-store repository and take its REST
   credentials; ``API_KEY``: from the user's free-form env block),
2. generates a random per-workspace ``RESTIC_PASSWORD`` and ``restic init``s
   the repo with it,
3. writes the canonical ``restic.env`` (repo + creds + random password) to
   the Imbue Studio store (see ``backup_env_store``), and
4. injects that whole file into the workspace at
   ``data/.secrets/restic.env`` via ``mngr exec``.

``CONFIGURE_LATER`` is a no-op. Re-provisioning is idempotent: if a
canonical env already exists for the workspace, Imbue Studio just re-injects it.
"""

import base64
import os
import secrets
import shlex
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import ENV_ARCHIVE_TIMESTAMP_FORMAT
from imbue.minds.desktop_client.backup_env_store import archive_canonical_env
from imbue.minds.desktop_client.backup_env_store import archived_password_for_repository
from imbue.minds.desktop_client.backup_env_store import canonical_env_path
from imbue.minds.desktop_client.backup_env_store import env_content_sha256
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryCredentials
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import ResticPasswordNotAllowedError
from imbue.minds.errors import WorkspaceFileWriteError
from imbue.minds.primitives import BackupProvider
from imbue.mngr.primitives import AgentId

_RESTIC_ENV_REMOTE_PATH = "data/.secrets/restic.env"
# Where a restore from a previous destination hands that destination's env to
# the restore program (relative to the agent work_dir, like restic.env).
RESTIC_PREVIOUS_ENV_REMOTE_PATH = "data/.secrets/restic.previous.env"
_RESTIC_ENV_MODE = "600"
# Bytes of entropy for the per-workspace repository password.
_WORKSPACE_PASSWORD_ENTROPY_BYTES = 32
# Hard cap for a single ``mngr exec`` round-trip into the workspace. A healthy
# host answers in well under a second; this bounds a single hung call so the
# surrounding retry loop (see ``AgentCreator._provision_backups``) can move on
# rather than blocking its whole budget on one stuck exec.
_MNGR_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0

_CANONICAL_ENV_HEADER = (
    "# Managed by Imbue Studio. Definitive copy of this machine's restic backup\n"
    "# configuration (repository + credentials + the machine's random\n"
    "# password). The copy inside the machine is injected from this file;\n"
    "# edit here and re-inject rather than editing the machine copy.\n"
)


class BackupSetupRequest(FrozenModel):
    """The inputs needed to configure backups for one host."""

    backup_provider: BackupProvider = Field(description="Which backup provider to configure")
    api_key_env_text: str = Field(
        default="",
        description=(
            "For API_KEY: the user's free-form KEY=VALUE block (RESTIC_REPOSITORY + backend creds). "
            "Must NOT define RESTIC_PASSWORD -- Imbue Studio assigns each workspace a random one."
        ),
    )
    account_email: str = Field(
        default="",
        description="For IMBUE_CLOUD: the account the repository is created under.",
    )


# Pure helpers


def _render_env_pairs(pairs: list[tuple[str, str]]) -> str:
    """Render KEY=value lines (one per pair, trailing newline each).

    Values are written unquoted: the host_backup ``parse_restic_env_file``
    reader takes everything after the first ``=`` and does no shell
    expansion, so unquoted is the faithful encoding for its consumer.
    """
    return "".join(f"{key}={value}\n" for key, value in pairs)


def env_text_defines_restic_password(env_text: str) -> bool:
    """Return whether a free-form env block assigns RESTIC_PASSWORD."""
    return "RESTIC_PASSWORD" in parse_restic_env(env_text)


def generate_workspace_password() -> str:
    """Generate a cryptographically random per-workspace restic password."""
    return secrets.token_urlsafe(_WORKSPACE_PASSWORD_ENTROPY_BYTES)


def build_canonical_env_content(
    *,
    repository: str,
    backend_env: dict[str, str],
    workspace_password: str,
) -> str:
    """Render the canonical restic.env (repo + backend creds + workspace password)."""
    pairs: list[tuple[str, str]] = [("RESTIC_REPOSITORY", repository)]
    pairs.extend((key, value) for key, value in backend_env.items())
    pairs.append(("RESTIC_PASSWORD", workspace_password))
    return _CANONICAL_ENV_HEADER + _render_env_pairs(pairs)


# Remote file injection (impure: drives `mngr exec` on the agent's host)


def build_backup_exec_argv(agent_address: str, command_str: str) -> list[str]:
    """The ``mngr exec`` argv for a backup-stack command on the agent's host.

    --no-start: ``mngr exec`` auto-starts a stopped host by default, and none of
    the backup-stack execs may cold-boot a container as a side effect -- the
    periodic verification check in particular gates on an ``is_workspace_online``
    reading that can be stale (a replayed pre-start RUNNING at app startup was
    observed booting a stopped container through this path).
    """
    return [MNGR_BINARY, "exec", agent_address, command_str, "--no-start"]


def run_mngr_exec_on_agent(
    agent_address: str,
    command_str: str,
    *,
    parent_cg: ConcurrencyGroup | None,
    timeout_seconds: float = _MNGR_EXEC_TIMEOUT_SECONDS,
    # Invoked with (line, is_stdout) for each output line as it arrives, so a
    # long-running command (a restore) can stream live progress into an
    # operation log.
    on_output: Callable[[str, bool], None] | None = None,
) -> FinishedProcess:
    """Run a single shell command on the agent's host via ``mngr exec``.

    Shared by env injection, the backup-service verification check, and the
    backup-service update scripts; the caller inspects the returned process.
    Never starts a stopped host (see :func:`build_backup_exec_argv`).
    """
    name = "backup-exec"
    cg = parent_cg.make_concurrency_group(name=name) if parent_cg is not None else ConcurrencyGroup(name=name)
    # When the caller wants live output, append ``--stream`` so ``mngr exec`` emits
    # each remote line as it arrives rather than buffering the whole command's
    # output and printing it only at the end (which made a long restore's
    # progress appear all at once). ``run_process_to_completion``'s own reader
    # then fires ``on_output`` per line; callers with no ``on_output`` (env
    # injection, gate probe, verification) stay on the non-streaming path.
    argv = build_backup_exec_argv(agent_address, command_str)
    if on_output is not None:
        argv = [*argv, "--stream"]
    with cg:
        return cg.run_process_to_completion(
            command=argv,
            timeout=timeout_seconds,
            is_checked_after=False,
            on_output=on_output,
        )


@pure
def build_write_file_command(remote_path: str, content: str, *, mode: str | None, rotate_timestamp: str | None) -> str:
    """The shell command that writes ``content`` to ``remote_path`` (relative to its cwd).

    The content is base64-encoded so arbitrary bytes (newlines, quotes,
    secrets) survive the shell round-trip intact. The base64 alphabet
    contains no shell-significant characters, so single-quoting it is safe.

    An existing file with identical content is left untouched (only its mode
    is enforced), so re-injecting the same restic.env keeps its mtime --
    host_backup starts a backup tick whenever that mtime moves. An existing
    file whose content differs is first moved aside to ``<path>.<timestamp>``
    so the old configuration stays recoverable; with ``rotate_timestamp``
    None it is overwritten in place (a side file nothing needs to recover).
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent_dir = os.path.dirname(remote_path) or "."
    quoted_path = shlex.quote(remote_path)
    new_sha = env_content_sha256(content)
    chmod = f"chmod {mode} {quoted_path}" if mode is not None else "true"
    if rotate_timestamp is None:
        move_aside = ""
    else:
        rotated_path = shlex.quote(f"{remote_path}.{rotate_timestamp}")
        move_aside = f"{{ [ ! -f {quoted_path} ] || mv {quoted_path} {rotated_path}; }} && "
    return (
        f"mkdir -p {shlex.quote(parent_dir)} && "
        f'if [ -f {quoted_path} ] && [ "$(sha256sum {quoted_path} | cut -d\' \' -f1)" = "{new_sha}" ]; '
        f"then {chmod}; "
        f"else {move_aside}"
        f"printf %s '{encoded}' | base64 -d > {quoted_path} && {chmod}; fi"
    )


def _write_remote_file(
    agent_address: str,
    remote_path: str,
    content: str,
    *,
    mode: str | None,
    parent_cg: ConcurrencyGroup | None,
    rotate_timestamp: str | None,
) -> None:
    """Write ``content`` to ``remote_path`` (relative to the agent work_dir) via mngr exec.

    See :func:`build_write_file_command` for what happens to an existing file.
    """
    command_str = build_write_file_command(remote_path, content, mode=mode, rotate_timestamp=rotate_timestamp)
    result = run_mngr_exec_on_agent(agent_address, command_str, parent_cg=parent_cg)
    if result.returncode != 0:
        raise WorkspaceFileWriteError(
            f"Failed to write {remote_path} on agent {agent_address}: {result.stderr.strip() or result.stdout.strip()}"
        )


def inject_previous_env_into_workspace(
    agent_address: str, content: str, *, parent_cg: ConcurrencyGroup | None
) -> None:
    """Write an archived restic.env beside the canonical one, for a restore from a previous destination.

    The restore program reads the repository to restore from at
    ``RESTIC_PREVIOUS_ENV_REMOTE_PATH`` and removes the file once read; the
    workspace's own ``restic.env`` (what host_backup ticks against) is never
    replaced. A copy left by an interrupted restore is overwritten, not
    rotated aside: nothing reads a stale one, and a credential should not
    linger under another name.
    """
    _write_remote_file(
        agent_address,
        RESTIC_PREVIOUS_ENV_REMOTE_PATH,
        content,
        mode=_RESTIC_ENV_MODE,
        parent_cg=parent_cg,
        rotate_timestamp=None,
    )


def _inject_canonical_env(agent_address: str, content: str, *, parent_cg: ConcurrencyGroup | None) -> None:
    """Inject the canonical restic.env into the workspace's data/.secrets/restic.env.

    An existing workspace copy with different content is rotated aside to
    ``restic.env.<timestamp>`` first, so the previous configuration is
    recoverable if something goes wrong.
    """
    _write_remote_file(
        agent_address,
        _RESTIC_ENV_REMOTE_PATH,
        content,
        mode=_RESTIC_ENV_MODE,
        parent_cg=parent_cg,
        rotate_timestamp=datetime.now(timezone.utc).strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT),
    )


# Repository provisioning (impure: drives `mngr imbue_cloud backup-repo ...`)


# Ceiling on quota evictions within one provisioning attempt: a backstop far
# above any real repository quota, so the evict-and-retry loop always terminates.
_MAX_QUOTA_EVICTIONS: Final[int] = 100


def _create_repository_evicting_on_quota(
    imbue_cloud_cli: ImbueCloudCli,
    account_email: str,
    workspace_id: str,
    quota_evictor: Callable[[], bool] | None,
) -> BackupRepositoryCredentials:
    """Create the workspace's backup-store repository and return its fresh credentials.

    The connector's create is idempotent (an existing repository gets a fresh
    token), so the same provisioning can be re-applied to a workspace whose
    repository was already created on an earlier run.

    On a quota rejection (repository count or storage bytes), ``quota_evictor``
    -- when provided -- destroys the oldest destroyed-workspace backup
    (something the reapers would delete anyway) and the create is retried,
    until it succeeds or nothing is left to evict. An evictor *failure*
    aborts immediately (propagates), so a broken destroy path cannot loop.
    """
    for _ in range(_MAX_QUOTA_EVICTIONS):
        try:
            return imbue_cloud_cli.create_backup_repository(account=account_email, workspace_id=workspace_id)
        except ImbueCloudQuotaExceededCliError:
            if quota_evictor is None or not quota_evictor():
                raise
            logger.info("Evicted a destroyed machine's backup to free quota; retrying the repository create")
    raise BackupProvisioningError(
        f"Repository create for {workspace_id} still over quota after {_MAX_QUOTA_EVICTIONS} evictions"
    )


def _resolve_repository_and_backend_env(
    request: BackupSetupRequest,
    workspace_id: str,
    *,
    imbue_cloud_cli: ImbueCloudCli | None,
    quota_evictor: Callable[[], bool] | None,
) -> tuple[str, dict[str, str]]:
    """Resolve ``(repository_url, backend_env)`` for the chosen provider.

    ``backend_env`` is the set of non-password env vars restic needs to reach
    the backend (``RESTIC_REST_USERNAME`` / ``RESTIC_REST_PASSWORD`` for the
    backup store, ``AWS_*`` and the like for a user's own backend); it never
    includes ``RESTIC_REPOSITORY`` or ``RESTIC_PASSWORD``.
    """
    if request.backup_provider is BackupProvider.IMBUE_CLOUD:
        if imbue_cloud_cli is None:
            raise BackupProvisioningError("imbue_cloud backups require imbue_cloud_cli to be configured")
        if not request.account_email:
            raise BackupProvisioningError("imbue_cloud backups require an account")
        if not workspace_id:
            raise BackupProvisioningError("imbue_cloud backups require a workspace id to name the repository")
        credentials = _create_repository_evicting_on_quota(
            imbue_cloud_cli, request.account_email, workspace_id, quota_evictor
        )
        backend_env = {
            "RESTIC_REST_USERNAME": credentials.rest_username,
            "RESTIC_REST_PASSWORD": credentials.rest_password.get_secret_value(),
        }
        return credentials.repository_url, backend_env
    if request.backup_provider is BackupProvider.API_KEY:
        env = parse_restic_env(request.api_key_env_text)
        if "RESTIC_PASSWORD" in env:
            raise ResticPasswordNotAllowedError(
                "RESTIC_PASSWORD must not be set for api_key backups; "
                "Imbue Studio assigns each workspace its own password"
            )
        repository = env.pop("RESTIC_REPOSITORY", "")
        if not repository:
            raise BackupProvisioningError("api_key backups require RESTIC_REPOSITORY to be set in the env block")
        return repository, env
    raise BackupProvisioningError(f"Unhandled backup provider: {request.backup_provider}")


# Orchestration


def configure_backups_for_host(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None = None,
    # Frees quota by force-destroying the oldest destroyed-workspace backup
    # when the repository create hits a quota limit; None disables eviction.
    quota_evictor: Callable[[], bool] | None = None,
    # False provisions fresh even when a canonical env exists: a destination
    # change must not re-inject an env the record sync put back meanwhile.
    is_existing_env_reused: bool = True,
) -> None:
    """Provision the repository (from Imbue Studio) and inject the workspace's restic.env.

    No-op for ``CONFIGURE_LATER``. Idempotent: an existing canonical env is
    just re-injected, unless ``is_existing_env_reused`` is False. Raises
    ``BackupProvisioningError`` (or the
    ``ResticNotInstalledError`` / ``ResticTooOldError`` subclasses) on
    failure so the caller can surface it as a notification; failure is
    non-fatal to the workspace.
    """
    if request.backup_provider is BackupProvider.CONFIGURE_LATER:
        logger.debug("Backup provider CONFIGURE_LATER for agent {}; nothing to do", agent_id)
        return

    with log_span("Configuring {} backups for agent {}", request.backup_provider.value, agent_id):
        # restic must be available on the Imbue Studio machine to init the repo.
        restic_cli.ensure_restic_available()

        # Idempotent re-provision: the canonical env is the source of truth.
        # If we already have it, the repo + key already exist -- just re-inject.
        existing_canonical = read_canonical_env(paths, agent_id) if is_existing_env_reused else None
        if existing_canonical is not None:
            logger.debug("Reusing existing canonical restic.env for agent {}; re-injecting", agent_id)
            _inject_canonical_env(agent_address, existing_canonical, parent_cg=parent_cg)
            return

        # Repositories are named by the workspace id: the backup is the
        # workspace's substrate-independent data, so its repository follows
        # the workspace, not the machine it happens to run on. Legacy
        # (bucket-backed) envs keep working through the idempotent
        # canonical-env reuse path above until the migration moves them.
        repository, backend_env = _resolve_repository_and_backend_env(
            request, str(agent_id), imbue_cloud_cli=imbue_cloud_cli, quota_evictor=quota_evictor
        )
        # A repository this workspace already keyed (an archived env names it:
        # a change rolled back after init, or backups turned off and on again)
        # only opens with that password, so it is reused rather than replaced.
        workspace_password = archived_password_for_repository(paths, agent_id, repository)
        if workspace_password is None:
            workspace_password = generate_workspace_password()
        else:
            logger.info("Reusing the password an archived restic.env of {} holds for its repository", agent_id)

        # Initialize the repo with the workspace's own random password -- its
        # single key. Cross-device and disaster-recovery access come from the
        # synced (encrypted) canonical env, not from extra repo keys.
        restic_cli.init_repo(
            repository=repository, backend_env=backend_env, password=workspace_password, parent_cg=parent_cg
        )

        canonical_env = build_canonical_env_content(
            repository=repository, backend_env=backend_env, workspace_password=workspace_password
        )
        # Persist the definitive copy first (so a later injection failure still
        # leaves Imbue Studio able to reach the repo / show status), then inject.
        write_canonical_env(paths, agent_id, canonical_env)
        _inject_canonical_env(agent_address, canonical_env, parent_cg=parent_cg)
        logger.debug("Injected restic backup config into agent {}", agent_id)


def reinject_canonical_env(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Re-inject the existing canonical env into the workspace (repair a drifted/missing copy).

    Raises ``BackupProvisioningError`` when no canonical env exists (nothing
    to repair) or the injection fails. A workspace copy with different content
    is rotated aside to ``restic.env.<timestamp>`` first.
    """
    canonical_env = read_canonical_env(paths, agent_id)
    if canonical_env is None:
        raise BackupProvisioningError(f"No canonical restic.env exists for {agent_id}; nothing to re-inject")
    _inject_canonical_env(agent_address, canonical_env, parent_cg=parent_cg)


def disable_backups_for_host(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Turn a workspace's backups off: archive the canonical env, rotate the workspace copy aside.

    The canonical env moves to the Imbue Studio archive (old snapshots stay
    reachable through it) and the workspace's ``restic.env`` is rotated to
    ``restic.env.<timestamp>`` -- a missing file means "not configured", so the
    host-backup service goes idle and the rotated copy cannot be re-adopted.
    Idempotent: disabling an already-disabled workspace is a no-op.
    """
    with log_span("Disabling backups for agent {}", agent_id):
        archived_path = archive_canonical_env(paths, agent_id, now=datetime.now(timezone.utc))
        if archived_path is not None:
            logger.info("Archived canonical restic.env for {} to {}", agent_id, archived_path.name)
        quoted_path = shlex.quote(_RESTIC_ENV_REMOTE_PATH)
        rotated_path = shlex.quote(
            f"{_RESTIC_ENV_REMOTE_PATH}.{datetime.now(timezone.utc).strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT)}"
        )
        command_str = f"if [ -f {quoted_path} ]; then mv {quoted_path} {rotated_path}; fi"
        result = run_mngr_exec_on_agent(agent_address, command_str, parent_cg=parent_cg)
        if result.returncode != 0:
            raise BackupProvisioningError(
                f"Failed to rotate {_RESTIC_ENV_REMOTE_PATH} aside on agent {agent_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )


def change_backup_destination_for_host(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None = None,
    quota_evictor: Callable[[], bool] | None = None,
    # When True, a change that fails after writing the new canonical env (at
    # injection, say) puts the archived env back as canonical, so the caller
    # can retry the whole change from scratch.
    is_rolled_back_on_failure: bool = False,
    # Injectable (a fake in tests); production runs the real provisioning.
    configure_fn: Callable[..., None] = configure_backups_for_host,
) -> None:
    """Point a workspace's backups at a new destination via fresh provisioning.

    Archives the existing canonical env on the Imbue Studio side (the old repository stays
    reachable through the archive), then runs the ordinary provisioning against
    the new inputs, told not to reuse a canonical env (the record sync may have
    put one back meanwhile): new random per-workspace password,
    ``restic init`` keyed solely by that password, canonical env write, and
    injection (which rotates the workspace copy).
    Existing snapshots stay in the old repository; the new destination starts
    fresh.
    """
    if request.backup_provider is BackupProvider.CONFIGURE_LATER:
        raise BackupProvisioningError("Changing the backup destination requires a real backup provider")
    archived_path = archive_canonical_env(paths, agent_id, now=datetime.now(timezone.utc))
    if archived_path is not None:
        logger.info("Archived previous canonical restic.env for {} to {}", agent_id, archived_path.name)
    is_changed = False
    try:
        configure_fn(
            agent_id=agent_id,
            agent_address=agent_address,
            request=request,
            imbue_cloud_cli=imbue_cloud_cli,
            paths=paths,
            parent_cg=parent_cg,
            quota_evictor=quota_evictor,
            is_existing_env_reused=False,
        )
        is_changed = True
    finally:
        # A change that produced no new canonical env (provisioning failed
        # before the write) must not leave the workspace with none at all: the
        # old destination is still the one its backups go to, so put its env
        # back and let the caller's error stand. A caller that retries from
        # scratch wants the same for a change that failed after the write.
        if (
            archived_path is not None
            and not is_changed
            and (is_rolled_back_on_failure or not has_canonical_env(paths, agent_id))
        ):
            _restore_archived_env(paths, archived_path, agent_id)


def _restore_archived_env(paths: InstallationPaths, archived_path: Path, agent_id: AgentId) -> None:
    """Put the archived env back as canonical, keeping any half-applied new env as an archive of its own.

    The half-applied env is the only copy of the password its repository was
    just keyed with; archived, the retry finds and reuses it
    (``archived_password_for_repository``).
    """
    half_applied_path = archive_canonical_env(paths, agent_id, now=_archive_time_after(archived_path))
    if half_applied_path is not None:
        logger.info("Kept the half-applied restic.env for {} as {}", agent_id, half_applied_path.name)
    try:
        archived_path.rename(canonical_env_path(paths, agent_id))
    except OSError as e:
        logger.warning("Could not restore the archived restic.env {} for {}: {}", archived_path.name, agent_id, e)
        return
    logger.info("Restored the previous canonical restic.env for {} after a failed destination change", agent_id)


def _archive_time_after(archived_path: Path) -> datetime:
    """Now, or one second past ``archived_path``'s timestamp: archive names carry whole seconds, so a
    second archive made within the same second would otherwise rename over the first."""
    archived_at = datetime.strptime(archived_path.name.rsplit(".", 1)[-1], ENV_ARCHIVE_TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc
    )
    return max(datetime.now(timezone.utc), archived_at + timedelta(seconds=1))
