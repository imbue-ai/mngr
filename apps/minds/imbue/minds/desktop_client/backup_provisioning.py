"""Configure restic backups for a workspace after its host is created.

This is the reusable "configure backups for host X" operation. It runs
asynchronously after ``mngr create`` returns the canonical host id (the
desktop client schedules it on a detached thread, mirroring the Cloudflare
tunnel-token injection), but nothing here is creation-specific: the same
entry point can be re-applied to any already-created host later.

The key idea: minds initializes the restic repository itself (from the
machine running minds) and gives each workspace its own random repository
password -- the repo's single key. Disaster recovery does not need a repo
"master key": the canonical env (and therefore the random password) syncs
inside the account's encrypted workspace record, unlocked by the master
password via the account DEK (see ``dek_store``). Concretely, enabling
backups:

1. resolves the repository URL + backend credentials (``IMBUE_CLOUD``:
   create/reuse a per-workspace R2 bucket + readwrite key; ``API_KEY``:
   from the user's free-form env block),
2. generates a random per-workspace ``RESTIC_PASSWORD`` and ``restic init``s
   the repo with it,
3. writes the canonical ``restic.env`` (repo + creds + random password) to
   the minds-side store (see ``backup_env_store``), and
4. injects that whole file into the workspace at
   ``runtime/secrets/restic.env`` via ``mngr exec``.

``CONFIGURE_LATER`` is a no-op. Re-provisioning is idempotent: if a
canonical env already exists for the workspace, minds just re-injects it.
"""

import base64
import os
import secrets
import shlex
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import ENV_ARCHIVE_TIMESTAMP_FORMAT
from imbue.minds.desktop_client.backup_env_store import archive_canonical_env
from imbue.minds.desktop_client.backup_env_store import env_content_sha256
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import R2BucketKeyMaterial
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider
from imbue.mngr.primitives import AgentId

_RESTIC_ENV_REMOTE_PATH = "runtime/secrets/restic.env"
_RESTIC_ENV_MODE = "600"
# Bytes of entropy for the per-workspace repository password.
_WORKSPACE_PASSWORD_ENTROPY_BYTES = 32
# Hard cap for a single ``mngr exec`` round-trip into the workspace. A healthy
# host answers in well under a second; this bounds a single hung call so the
# surrounding retry loop (see ``AgentCreator._provision_backups``) can move on
# rather than blocking its whole budget on one stuck exec.
_MNGR_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0

_CANONICAL_ENV_HEADER = (
    "# Managed by minds. Definitive copy of this workspace's restic backup\n"
    "# configuration (repository + credentials + the workspace's random\n"
    "# password). The copy inside the workspace is injected from this file;\n"
    "# edit here and re-inject rather than editing the workspace copy.\n"
)


class BackupSetupRequest(FrozenModel):
    """The inputs needed to configure backups for one host."""

    backup_provider: BackupProvider = Field(description="Which backup provider to configure")
    api_key_env_text: str = Field(
        default="",
        description=(
            "For API_KEY: the user's free-form KEY=VALUE block (RESTIC_REPOSITORY + backend creds). "
            "Must NOT define RESTIC_PASSWORD -- minds assigns each workspace a random one."
        ),
    )
    account_email: str = Field(
        default="",
        description="For IMBUE_CLOUD: the account the bucket is created under.",
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Remote file injection (impure: drives `mngr exec` on the agent's host)
# ---------------------------------------------------------------------------


def run_mngr_exec_on_agent(
    agent_id: AgentId,
    command_str: str,
    *,
    parent_cg: ConcurrencyGroup | None,
    timeout_seconds: float = _MNGR_EXEC_TIMEOUT_SECONDS,
) -> FinishedProcess:
    """Run a single shell command on the agent's host via ``mngr exec``.

    Shared by env injection, the backup-service verification check, and the
    backup-service update scripts; the caller inspects the returned process.
    """
    name = "backup-exec"
    cg = parent_cg.make_concurrency_group(name=name) if parent_cg is not None else ConcurrencyGroup(name=name)
    with cg:
        return cg.run_process_to_completion(
            command=[MNGR_BINARY, "exec", str(agent_id), command_str],
            timeout=timeout_seconds,
            is_checked_after=False,
        )


def _write_remote_file(
    agent_id: AgentId,
    remote_path: str,
    content: str,
    *,
    mode: str | None,
    parent_cg: ConcurrencyGroup | None,
    rotate_timestamp: str | None = None,
) -> None:
    """Write ``content`` to ``remote_path`` (relative to the agent work_dir) via mngr exec.

    The content is base64-encoded so arbitrary bytes (newlines, quotes,
    secrets) survive the shell round-trip intact. The base64 alphabet
    contains no shell-significant characters, so single-quoting it is safe.

    When ``rotate_timestamp`` is given, an existing file whose content differs
    from the new content is first moved aside to ``<path>.<timestamp>`` so the
    old configuration stays recoverable; an identical existing file is left in
    place unrotated (idempotent re-injection must not accumulate copies).
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent_dir = os.path.dirname(remote_path) or "."
    quoted_path = shlex.quote(remote_path)
    chmod_suffix = f" && chmod {mode} {quoted_path}" if mode is not None else ""
    rotate_prefix = ""
    if rotate_timestamp is not None:
        new_sha = env_content_sha256(content)
        rotated_path = shlex.quote(f"{remote_path}.{rotate_timestamp}")
        rotate_prefix = (
            f'if [ -f {quoted_path} ] && [ "$(sha256sum {quoted_path} | cut -d\' \' -f1)" != "{new_sha}" ]; '
            f"then mv {quoted_path} {rotated_path}; fi && "
        )
    command_str = (
        f"mkdir -p {shlex.quote(parent_dir)} && "
        f"{rotate_prefix}"
        f"printf %s '{encoded}' | base64 -d > {quoted_path}{chmod_suffix}"
    )
    result = run_mngr_exec_on_agent(agent_id, command_str, parent_cg=parent_cg)
    if result.returncode != 0:
        raise BackupProvisioningError(
            f"Failed to write {remote_path} on agent {agent_id}: {result.stderr.strip() or result.stdout.strip()}"
        )


def _inject_canonical_env(agent_id: AgentId, content: str, *, parent_cg: ConcurrencyGroup | None) -> None:
    """Inject the canonical restic.env into the workspace's runtime/secrets/restic.env.

    An existing workspace copy with different content is rotated aside to
    ``restic.env.<timestamp>`` first, so the previous configuration is
    recoverable if something goes wrong.
    """
    _write_remote_file(
        agent_id,
        _RESTIC_ENV_REMOTE_PATH,
        content,
        mode=_RESTIC_ENV_MODE,
        parent_cg=parent_cg,
        rotate_timestamp=datetime.now(timezone.utc).strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT),
    )


# ---------------------------------------------------------------------------
# Bucket provisioning (impure: drives `mngr imbue_cloud bucket ...`)
# ---------------------------------------------------------------------------


def _is_bucket_already_exists_error(error: ImbueCloudCliError) -> bool:
    """Return whether an imbue_cloud CLI error means the bucket already exists.

    Prefers the structured signal: the plugin's CLI writes the raising
    exception's class name into the stderr JSON ("error_class"), so a
    ``ImbueCloudBucketExistsError`` is detectable independently of the
    (re-wordable) human-readable detail. The prose ``already exists`` match
    is kept as a fallback for older / differently-shaped error bodies.
    """
    haystack = f"{error.stderr} {error}".lower()
    return "imbuecloudbucketexistserror" in haystack or "already exists" in haystack


def _create_or_reuse_bucket(
    imbue_cloud_cli: ImbueCloudCli,
    account_email: str,
    bucket_short_name: str,
) -> tuple[str, str, R2BucketKeyMaterial]:
    """Create the per-workspace bucket, or reuse it (minting a fresh key) if it exists.

    Returns ``(bucket_name, s3_endpoint, key_material)``. Idempotent so the
    same provisioning can be re-applied to a host whose bucket was already
    created on an earlier run.
    """
    try:
        result = imbue_cloud_cli.create_bucket(account=account_email, name=bucket_short_name, access="readwrite")
        return result.bucket.bucket_name, str(result.bucket.s3_endpoint), result.key
    except ImbueCloudCliError as e:
        if not _is_bucket_already_exists_error(e):
            raise
        logger.debug("Bucket {} already exists; reusing it with a fresh key", bucket_short_name)
        info = imbue_cloud_cli.get_bucket_info(account_email, bucket_short_name)
        key = imbue_cloud_cli.create_bucket_key(account=account_email, name=bucket_short_name, access="readwrite")
        return info.bucket_name, str(info.s3_endpoint), key


def _repository_url_for_bucket(s3_endpoint: str, bucket_name: str) -> str:
    """Build the restic S3 repository URL pointing at the bucket root."""
    return f"s3:{s3_endpoint.rstrip('/')}/{bucket_name}"


def _resolve_repository_and_backend_env(
    request: BackupSetupRequest,
    host_id: str,
    *,
    imbue_cloud_cli: ImbueCloudCli | None,
) -> tuple[str, dict[str, str]]:
    """Resolve ``(repository_url, backend_env)`` for the chosen provider.

    ``backend_env`` is the set of non-password env vars restic needs to reach
    the backend (e.g. ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``); it
    never includes ``RESTIC_REPOSITORY`` or ``RESTIC_PASSWORD``.
    """
    if request.backup_provider is BackupProvider.IMBUE_CLOUD:
        if imbue_cloud_cli is None:
            raise BackupProvisioningError("imbue_cloud backups require imbue_cloud_cli to be configured")
        if not request.account_email:
            raise BackupProvisioningError("imbue_cloud backups require an account")
        if not host_id:
            raise BackupProvisioningError("imbue_cloud backups require a host id to name the bucket")
        bucket_name, s3_endpoint, key = _create_or_reuse_bucket(imbue_cloud_cli, request.account_email, host_id)
        repository = _repository_url_for_bucket(s3_endpoint, bucket_name)
        backend_env = {
            "AWS_ACCESS_KEY_ID": str(key.access_key_id),
            "AWS_SECRET_ACCESS_KEY": key.secret_access_key.get_secret_value(),
        }
        return repository, backend_env
    if request.backup_provider is BackupProvider.API_KEY:
        env = parse_restic_env(request.api_key_env_text)
        if "RESTIC_PASSWORD" in env:
            raise BackupProvisioningError(
                "RESTIC_PASSWORD must not be set for api_key backups; minds assigns each workspace its own password"
            )
        repository = env.pop("RESTIC_REPOSITORY", "")
        if not repository:
            raise BackupProvisioningError("api_key backups require RESTIC_REPOSITORY to be set in the env block")
        return repository, env
    raise BackupProvisioningError(f"Unhandled backup provider: {request.backup_provider}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def configure_backups_for_host(
    *,
    agent_id: AgentId,
    host_id: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: WorkspacePaths,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Provision the repository (from minds) and inject the workspace's restic.env.

    No-op for ``CONFIGURE_LATER``. Idempotent: an existing canonical env is
    just re-injected. Raises ``BackupProvisioningError`` (or the
    ``ResticNotInstalledError`` subclass) on failure so the caller can
    surface it as a notification; failure is non-fatal to the workspace.
    """
    if request.backup_provider is BackupProvider.CONFIGURE_LATER:
        logger.debug("Backup provider CONFIGURE_LATER for agent {}; nothing to do", agent_id)
        return

    with log_span("Configuring {} backups for agent {}", request.backup_provider.value, agent_id):
        # restic must be available on the minds machine to init the repo.
        restic_cli.ensure_restic_available()

        # Idempotent re-provision: the canonical env is the source of truth.
        # If we already have it, the repo + key already exist -- just re-inject.
        existing_canonical = read_canonical_env(paths, agent_id)
        if existing_canonical is not None:
            logger.debug("Reusing existing canonical restic.env for agent {}; re-injecting", agent_id)
            _inject_canonical_env(agent_id, existing_canonical, parent_cg=parent_cg)
            return

        repository, backend_env = _resolve_repository_and_backend_env(
            request, host_id, imbue_cloud_cli=imbue_cloud_cli
        )
        workspace_password = generate_workspace_password()

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
        # leaves minds able to reach the repo / show status), then inject.
        write_canonical_env(paths, agent_id, canonical_env)
        _inject_canonical_env(agent_id, canonical_env, parent_cg=parent_cg)
        logger.debug("Injected restic backup config into agent {}", agent_id)


def reinject_canonical_env(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
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
    _inject_canonical_env(agent_id, canonical_env, parent_cg=parent_cg)


def disable_backups_for_host(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Turn a workspace's backups off: archive the canonical env, rotate the workspace copy aside.

    The canonical env moves to the minds-side archive (old snapshots stay
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
        result = run_mngr_exec_on_agent(agent_id, command_str, parent_cg=parent_cg)
        if result.returncode != 0:
            raise BackupProvisioningError(
                f"Failed to rotate {_RESTIC_ENV_REMOTE_PATH} aside on agent {agent_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )


def change_backup_destination_for_host(
    *,
    agent_id: AgentId,
    host_id: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: WorkspacePaths,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Point a workspace's backups at a new destination via fresh provisioning.

    Archives the existing canonical env minds-side (the old repository stays
    reachable through the archive), then runs the ordinary idempotent
    provisioning against the new inputs: new random per-workspace password,
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
    configure_backups_for_host(
        agent_id=agent_id,
        host_id=host_id,
        request=request,
        imbue_cloud_cli=imbue_cloud_cli,
        paths=paths,
        parent_cg=parent_cg,
    )
