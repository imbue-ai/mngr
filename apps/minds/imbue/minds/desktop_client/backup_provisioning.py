"""Configure restic backups for a workspace after its host is created.

This is the reusable "configure backups for host X" operation. It runs
asynchronously after ``mngr create`` returns the canonical host id (the
desktop client schedules it on a detached thread, mirroring the
Cloudflare tunnel-token injection), but nothing here is creation-specific:
the same entry point can be re-applied to any already-created host later.

For the ``IMBUE_CLOUD`` backup provider it creates (or reuses) a
per-workspace R2 bucket named after the host id, mints a readwrite key,
and injects a ``runtime/secrets/restic.env`` pointing restic at that
bucket. For ``API_KEY`` it writes the user's free-form env block verbatim.
``CONFIGURE_LATER`` is a no-op. When the chosen encryption method is
"no password", it also flips ``restic.allow_empty_password`` in the
host's ``runtime/backup.toml`` so the host_backup service runs restic with
``--insecure-no-password``.

The two on-disk files are the contract consumed by the FCT ``host_backup``
service: ``runtime/secrets/restic.env`` (repository URL + credentials) and
``runtime/backup.toml`` (non-secret knobs, here just the empty-password
toggle).
"""

import base64
import os
import shlex

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import SecretStr
from tomlkit.exceptions import TOMLKitError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import R2BucketKeyMaterial
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider
from imbue.mngr.primitives import AgentId

_RESTIC_ENV_REMOTE_PATH = "runtime/secrets/restic.env"
_BACKUP_TOML_REMOTE_PATH = "runtime/backup.toml"
_RESTIC_ENV_MODE = "600"

_IMBUE_CLOUD_ENV_HEADER = (
    "# Managed by minds: restic backup repository + credentials for the\n"
    "# imbue_cloud bucket created for this workspace. Edit backup.toml for\n"
    "# non-secret knobs (retention, excludes, allow_empty_password).\n"
)


class BackupSetupRequest(FrozenModel):
    """The inputs needed to configure backups for one host."""

    backup_provider: BackupProvider = Field(description="Which backup provider to configure")
    master_password: SecretStr | None = Field(
        default=None,
        description=(
            "Resolved restic repository passphrase to inject as RESTIC_PASSWORD. None means "
            "the empty-password path (restic --insecure-no-password). The caller maps the "
            "encryption-method choice to this value."
        ),
    )
    api_key_env_text: str = Field(
        default="",
        description="For API_KEY: the user's free-form KEY=VALUE block, written verbatim to restic.env.",
    )
    account_email: str = Field(
        default="",
        description="For IMBUE_CLOUD: the account the bucket is created under.",
    )


class BackupInjectionPlan(FrozenModel):
    """The concrete files to write to the host, computed from a request."""

    restic_env_content: str = Field(description="Full contents of runtime/secrets/restic.env")
    allow_empty_password: bool = Field(
        description="When true, restic.allow_empty_password is set in backup.toml (empty-password repo)"
    )


# ---------------------------------------------------------------------------
# Pure plan computation
# ---------------------------------------------------------------------------


def _render_env_pairs(pairs: list[tuple[str, str]]) -> str:
    """Render KEY=value lines (one per pair, trailing newline each).

    Values are written unquoted: the host_backup ``parse_restic_env_file``
    reader takes everything after the first ``=`` and does no shell
    expansion, so unquoted is the faithful encoding for its consumer.
    """
    return "".join(f"{key}={value}\n" for key, value in pairs)


def _defined_env_keys(env_text: str) -> set[str]:
    """Return the set of KEY names assigned in a KEY=VALUE env block.

    Mirrors the envelope contract of host_backup's ``parse_restic_env_file``
    (supports leading ``export``, ignores comments / blanks / keyless lines).
    """
    keys: set[str] = set()
    for raw_line in env_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def env_text_defines_restic_password(env_text: str) -> bool:
    """Return whether the env block already assigns RESTIC_PASSWORD."""
    return "RESTIC_PASSWORD" in _defined_env_keys(env_text)


def build_imbue_cloud_restic_env(
    *,
    repository: str,
    access_key_id: str,
    secret_access_key: str,
    master_password: str | None,
) -> BackupInjectionPlan:
    """Build the restic.env plan for an imbue_cloud R2 bucket backup."""
    pairs: list[tuple[str, str]] = [
        ("RESTIC_REPOSITORY", repository),
        ("AWS_ACCESS_KEY_ID", access_key_id),
        ("AWS_SECRET_ACCESS_KEY", secret_access_key),
    ]
    if master_password is not None:
        pairs.append(("RESTIC_PASSWORD", master_password))
    content = _IMBUE_CLOUD_ENV_HEADER + _render_env_pairs(pairs)
    return BackupInjectionPlan(restic_env_content=content, allow_empty_password=master_password is None)


def build_api_key_restic_env(*, env_text: str, master_password: str | None) -> BackupInjectionPlan:
    """Build the restic.env plan for the API_KEY (free-form) provider.

    The textarea is written verbatim. The password it contains (if any)
    wins: an explicit ``RESTIC_PASSWORD`` in the block is left as-is and the
    encryption-method choice is ignored. Otherwise ``master_password``
    governs -- a value is appended as ``RESTIC_PASSWORD``; ``None`` means
    an empty-password repo.
    """
    normalized = env_text if (not env_text or env_text.endswith("\n")) else env_text + "\n"
    if env_text_defines_restic_password(env_text):
        return BackupInjectionPlan(restic_env_content=normalized, allow_empty_password=False)
    if master_password is not None:
        content = normalized + _render_env_pairs([("RESTIC_PASSWORD", master_password)])
        return BackupInjectionPlan(restic_env_content=content, allow_empty_password=False)
    return BackupInjectionPlan(restic_env_content=normalized, allow_empty_password=True)


def merge_allow_empty_password_into_backup_toml(existing_text: str, value: bool) -> str:
    """Return ``existing_text`` with ``[restic] allow_empty_password = value`` set.

    Preserves every other section (snapshot, retention, excludes, ...) that
    bootstrap wrote. When ``existing_text`` is blank (backup.toml not yet
    seeded), produces a minimal document with just the ``[restic]`` table;
    bootstrap fills in ``[snapshot]`` on its next boot/merge.
    """
    try:
        doc = tomlkit.parse(existing_text) if existing_text.strip() else tomlkit.document()
    except TOMLKitError as e:
        raise BackupProvisioningError(f"Could not parse existing backup.toml: {e}") from e
    if "restic" in doc:
        restic_table = doc["restic"]
    else:
        restic_table = tomlkit.table()
        doc["restic"] = restic_table
    restic_table["allow_empty_password"] = value
    return tomlkit.dumps(doc)


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


def _read_remote_file(
    agent_id: AgentId,
    remote_path: str,
    *,
    parent_cg: ConcurrencyGroup | None,
) -> str:
    """Read ``remote_path`` from the agent work_dir; return "" if it is absent."""
    command_str = f"cat {shlex.quote(remote_path)} 2>/dev/null || true"
    result = _run_mngr_exec(agent_id, command_str, parent_cg=parent_cg)
    if result.returncode != 0:
        raise BackupProvisioningError(
            f"Failed to read {remote_path} on agent {agent_id}: {result.stderr.strip()}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Bucket provisioning (impure: drives `mngr imbue_cloud bucket ...`)
# ---------------------------------------------------------------------------


def _is_bucket_already_exists_error(error: ImbueCloudCliError) -> bool:
    """Return whether an imbue_cloud CLI error means the bucket already exists."""
    haystack = f"{error.stderr} {error}".lower()
    return "already exists" in haystack


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


def _plan_for_request(
    request: BackupSetupRequest,
    host_id: str,
    *,
    imbue_cloud_cli: ImbueCloudCli | None,
) -> BackupInjectionPlan | None:
    """Compute the injection plan for a request, provisioning a bucket if needed.

    Returns None for ``CONFIGURE_LATER`` (nothing to inject).
    """
    master_password = request.master_password.get_secret_value() if request.master_password is not None else None
    if request.backup_provider is BackupProvider.CONFIGURE_LATER:
        return None
    if request.backup_provider is BackupProvider.API_KEY:
        return build_api_key_restic_env(env_text=request.api_key_env_text, master_password=master_password)
    if request.backup_provider is BackupProvider.IMBUE_CLOUD:
        if imbue_cloud_cli is None:
            raise BackupProvisioningError("imbue_cloud backups require imbue_cloud_cli to be configured")
        if not request.account_email:
            raise BackupProvisioningError("imbue_cloud backups require an account")
        if not host_id:
            raise BackupProvisioningError("imbue_cloud backups require a host id to name the bucket")
        bucket_name, s3_endpoint, key = _create_or_reuse_bucket(imbue_cloud_cli, request.account_email, host_id)
        return build_imbue_cloud_restic_env(
            repository=_repository_url_for_bucket(s3_endpoint, bucket_name),
            access_key_id=key.access_key_id,
            secret_access_key=key.secret_access_key.get_secret_value(),
            master_password=master_password,
        )
    raise BackupProvisioningError(f"Unhandled backup provider: {request.backup_provider}")


def configure_backups_for_host(
    *,
    agent_id: AgentId,
    host_id: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    parent_cg: ConcurrencyGroup | None = None,
) -> None:
    """Provision + inject restic backup config for a created host.

    No-op for ``CONFIGURE_LATER``. Raises ``BackupProvisioningError`` on any
    failure so the caller can surface it (the desktop client turns it into a
    notification); it is non-fatal to the already-created workspace.
    """
    with log_span("Configuring {} backups for agent {}", request.backup_provider.value, agent_id):
        plan = _plan_for_request(request, host_id, imbue_cloud_cli=imbue_cloud_cli)
        if plan is None:
            logger.debug("Backup provider CONFIGURE_LATER for agent {}; nothing to inject", agent_id)
            return
        _write_remote_file(
            agent_id,
            _RESTIC_ENV_REMOTE_PATH,
            plan.restic_env_content,
            mode=_RESTIC_ENV_MODE,
            parent_cg=parent_cg,
        )
        if plan.allow_empty_password:
            existing_backup_toml = _read_remote_file(agent_id, _BACKUP_TOML_REMOTE_PATH, parent_cg=parent_cg)
            merged = merge_allow_empty_password_into_backup_toml(existing_backup_toml, True)
            _write_remote_file(agent_id, _BACKUP_TOML_REMOTE_PATH, merged, mode=None, parent_cg=parent_cg)
        logger.debug("Injected restic backup config into agent {}", agent_id)
