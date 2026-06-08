"""Configure restic backups for a workspace after its host is created.

This is the reusable "configure backups for host X" operation. It runs
asynchronously after ``mngr create`` returns the canonical host id (the
desktop client schedules it on a detached thread, mirroring the Cloudflare
tunnel-token injection), but nothing here is creation-specific: the same
entry point can be re-applied to any already-created host later.

The key idea: minds initializes the restic repository itself (from the
machine running minds) and gives each workspace its own random repository
password, so the workspace never holds the user's master password and
carries no repo-init logic. Concretely, enabling backups:

1. resolves the repository URL + backend credentials (``IMBUE_CLOUD``:
   create/reuse a per-workspace R2 bucket + readwrite key; ``API_KEY``:
   from the user's free-form env block),
2. generates a random per-workspace ``RESTIC_PASSWORD``,
3. ``restic init``s the repo using the user's master password (or empty for
   the ``no_password`` encryption method),
4. ``restic key add``s the random per-workspace password,
5. writes the canonical ``restic.env`` (repo + creds + random password) to
   the minds-side store (see ``backup_env_store``), and
6. injects that whole file into the workspace at
   ``runtime/secrets/restic.env`` via ``mngr exec``.

``CONFIGURE_LATER`` is a no-op. Re-provisioning is idempotent: if a
canonical env already exists for the workspace, minds just re-injects it.
"""

import base64
import os
import secrets
import shlex
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
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
    master_password: SecretStr | None = Field(
        default=None,
        description=(
            "The user's master/recovery password used (only) to `restic init` the repo. None means "
            "the no_password encryption method -- the repo is initialized with an empty password. "
            "This is never written into the workspace; the workspace gets its own random password."
        ),
    )
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


def _run_mngr_exec(
    agent_id: AgentId,
    command_str: str,
    *,
    parent_cg: ConcurrencyGroup | None,
) -> FinishedProcess:
    """Run a single shell command on the agent's host via ``mngr exec``."""
    name = "backup-inject"
    cg = parent_cg.make_concurrency_group(name=name) if parent_cg is not None else ConcurrencyGroup(name=name)
    with cg:
        return cg.run_process_to_completion(
            command=[MNGR_BINARY, "exec", str(agent_id), command_str],
            timeout=_MNGR_EXEC_TIMEOUT_SECONDS,
            is_checked_after=False,
        )


def _write_remote_file(
    agent_id: AgentId,
    remote_path: str,
    content: str,
    *,
    mode: str | None,
    parent_cg: ConcurrencyGroup | None,
) -> None:
    """Write ``content`` to ``remote_path`` (relative to the agent work_dir) via mngr exec.

    The content is base64-encoded so arbitrary bytes (newlines, quotes,
    secrets) survive the shell round-trip intact. The base64 alphabet
    contains no shell-significant characters, so single-quoting it is safe.
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    parent_dir = os.path.dirname(remote_path) or "."
    chmod_suffix = f" && chmod {mode} {shlex.quote(remote_path)}" if mode is not None else ""
    command_str = (
        f"mkdir -p {shlex.quote(parent_dir)} && "
        f"printf %s '{encoded}' | base64 -d > {shlex.quote(remote_path)}{chmod_suffix}"
    )
    result = _run_mngr_exec(agent_id, command_str, parent_cg=parent_cg)
    if result.returncode != 0:
        raise BackupProvisioningError(
            f"Failed to write {remote_path} on agent {agent_id}: {result.stderr.strip() or result.stdout.strip()}"
        )


def _inject_canonical_env(agent_id: AgentId, content: str, *, parent_cg: ConcurrencyGroup | None) -> None:
    """Inject the canonical restic.env into the workspace's runtime/secrets/restic.env."""
    _write_remote_file(agent_id, _RESTIC_ENV_REMOTE_PATH, content, mode=_RESTIC_ENV_MODE, parent_cg=parent_cg)


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
        # restic must be available on the minds machine to init the repo + add the key.
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
        master_password = request.master_password.get_secret_value() if request.master_password is not None else None
        workspace_password = generate_workspace_password()

        # Initialize the repo with the master (or empty) password, then add the
        # random per-workspace password as an additional key. The workspace only
        # ever receives the random password.
        restic_cli.init_repo(
            repository=repository, backend_env=backend_env, password=master_password, parent_cg=parent_cg
        )
        restic_cli.add_password_key(
            repository=repository,
            backend_env=backend_env,
            existing_password=master_password,
            new_password=workspace_password,
            parent_cg=parent_cg,
        )

        canonical_env = build_canonical_env_content(
            repository=repository, backend_env=backend_env, workspace_password=workspace_password
        )
        # Persist the definitive copy first (so a later injection failure still
        # leaves minds able to reach the repo / show status), then inject.
        write_canonical_env(paths, agent_id, canonical_env)
        _inject_canonical_env(agent_id, canonical_env, parent_cg=parent_cg)
        logger.debug("Injected restic backup config into agent {}", agent_id)
