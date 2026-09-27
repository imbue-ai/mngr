"""Canonical per-workspace restic env files, owned by the minds app.

minds is the source of truth for how to reach each workspace's restic
repository. For every workspace with backups configured, minds keeps the
definitive ``restic.env`` (repository URL + backend credentials + the
workspace's random ``RESTIC_PASSWORD``) here, 0600, under the minds env's
data dir. The copy inside the workspace at ``data/.secrets/restic.env``
is just an injected mirror of this file; config changes are made here and
re-injected whole.

These files are never auto-deleted -- not even on workspace destroy -- so a
stopped or destroyed workspace's backups stay reachable for status checks
and restores.
"""

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_BACKUP_ENV_DIRNAME = "backup_envs"
# Timestamp format for archived canonical envs (and the workspace-side
# `restic.env.<date>` rotation) -- filesystem-safe, sortable, UTC.
ENV_ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def backup_env_dir(paths: InstallationPaths) -> Path:
    """Return the directory holding the canonical per-workspace restic env files."""
    return paths.data_dir / _BACKUP_ENV_DIRNAME


def canonical_env_path(paths: InstallationPaths, agent_id: AgentId) -> Path:
    """Return the path of the canonical restic.env for ``agent_id``."""
    return backup_env_dir(paths) / f"{agent_id}.env"


def has_canonical_env(paths: InstallationPaths, agent_id: AgentId) -> bool:
    """Return whether a canonical restic.env exists for ``agent_id``."""
    return canonical_env_path(paths, agent_id).is_file()


def read_canonical_env(paths: InstallationPaths, agent_id: AgentId) -> str | None:
    """Return the canonical restic.env contents for ``agent_id``, or None if absent."""
    path = canonical_env_path(paths, agent_id)
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except OSError as e:
        raise BackupProvisioningError(f"Could not read canonical restic.env at {path}: {e}") from e


def write_canonical_env(paths: InstallationPaths, agent_id: AgentId, content: str) -> None:
    """Write (overwriting) the canonical restic.env for ``agent_id``, mode 0600."""
    path = canonical_env_path(paths, agent_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically with tight perms: write to a 0600 temp file then
        # rename over the target so a reader never sees a partial or
        # world-readable secret.
        tmp_path = path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        tmp_path.rename(path)
    except OSError as e:
        raise BackupProvisioningError(f"Could not write canonical restic.env at {path}: {e}") from e


def env_content_sha256(content: str) -> str:
    """Hex sha256 of an env file's exact bytes (the drift-comparison currency)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def archive_canonical_env(paths: InstallationPaths, agent_id: AgentId, *, now: datetime) -> Path | None:
    """Move the canonical restic.env aside to ``<agent_id>.env.<timestamp>``.

    Used before re-provisioning a workspace against a new destination so the
    old repository stays reachable through the archived copy. Returns the
    archive path, or None when no canonical env exists.
    """
    path = canonical_env_path(paths, agent_id)
    if not path.is_file():
        return None
    archive_path = path.with_name(f"{path.name}.{now.strftime(ENV_ARCHIVE_TIMESTAMP_FORMAT)}")
    try:
        path.rename(archive_path)
    except OSError as e:
        raise BackupProvisioningError(f"Could not archive canonical restic.env at {path}: {e}") from e
    return archive_path


# The R2 endpoint marker identifying an imbue_cloud-provisioned repository.
_R2_ENDPOINT_MARKER: Final[str] = ".r2.cloudflarestorage.com"


@pure
def split_backup_bucket_name_from_env(env_content: str) -> tuple[str, str] | None:
    """``(owner_prefix, short_name)`` of the backup bucket from a canonical env, or None for BYO backends.

    imbue_cloud repositories look like
    ``s3:https://<acct>.r2.cloudflarestorage.com/<owner-prefix>--<short-name>``;
    anything without the R2 endpoint marker (or the owner-prefix separator) is
    a bring-your-own backend, which has no minds-managed bucket.
    """
    repository = parse_restic_env(env_content).get("RESTIC_REPOSITORY", "")
    if _R2_ENDPOINT_MARKER not in repository:
        return None
    bucket_name = repository.rstrip("/").rsplit("/", 1)[-1]
    if "--" not in bucket_name:
        return None
    owner_prefix, _, short_name = bucket_name.partition("--")
    return owner_prefix, short_name


@pure
def full_backup_bucket_name_from_env(env_content: str) -> str | None:
    """The full R2 backup bucket name from a canonical env, or None for BYO backends."""
    parsed = split_backup_bucket_name_from_env(env_content)
    if parsed is None:
        return None
    owner_prefix, short_name = parsed
    return f"{owner_prefix}--{short_name}"


def parse_restic_env(content: str) -> dict[str, str]:
    """Parse a KEY=value restic env block into a dict.

    Mirrors the host_backup ``parse_restic_env_file`` envelope: supports a
    leading ``export``, strips one layer of matched surrounding quotes,
    ignores comments / blanks / keyless lines, and performs no shell
    expansion.
    """
    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result
