"""Canonical per-workspace restic env files, owned by the Imbue Studio app.

Imbue Studio is the source of truth for how to reach each workspace's restic
repository. For every workspace with backups configured, Imbue Studio keeps the
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
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_BACKUP_ENV_DIRNAME = "backup_envs"
# Timestamp format for archived canonical envs (and the workspace-side
# `restic.env.<date>` rotation) -- filesystem-safe, sortable, UTC.
ENV_ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# An archive id is exactly one such timestamp, so a client-supplied id can
# only ever name a file inside the env directory.
_ENV_ARCHIVE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}Z$")


class ArchivedBackupEnv(FrozenModel):
    """One archived canonical restic.env of a workspace (a previous backup destination)."""

    archive_id: str = Field(description="The archive timestamp suffix; names the archive in API routes")
    archived_at: datetime = Field(description="When the env was archived (parsed from the suffix, UTC)")
    path: Path = Field(description="The archive file")


def backup_env_dir(paths: InstallationPaths) -> Path:
    """Return the directory holding the canonical per-workspace restic env files."""
    return paths.data_dir / _BACKUP_ENV_DIRNAME


def canonical_env_path(paths: InstallationPaths, agent_id: AgentId) -> Path:
    """Return the path of the canonical restic.env for ``agent_id``."""
    return backup_env_dir(paths) / f"{agent_id}.env"


def has_canonical_env(paths: InstallationPaths, agent_id: AgentId) -> bool:
    """Return whether a canonical restic.env exists for ``agent_id``."""
    return canonical_env_path(paths, agent_id).is_file()


def has_archived_env(paths: InstallationPaths, agent_id: AgentId) -> bool:
    """Return whether a canonical restic.env for ``agent_id`` was ever archived aside (see :func:`archive_canonical_env`)."""
    return bool(list_archived_envs(paths, agent_id))


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


def list_archived_envs(paths: InstallationPaths, agent_id: AgentId) -> list[ArchivedBackupEnv]:
    """The workspace's archived canonical envs, newest first (previous backup destinations)."""
    env_dir = backup_env_dir(paths)
    if not env_dir.is_dir():
        return []
    archives: list[ArchivedBackupEnv] = []
    for archive_path in env_dir.glob(f"{agent_id}.env.*"):
        archive_id = archive_path.name.rsplit(".", 1)[-1]
        if not _ENV_ARCHIVE_ID_RE.match(archive_id):
            continue
        archived_at = datetime.strptime(archive_id, ENV_ARCHIVE_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        archives.append(ArchivedBackupEnv(archive_id=archive_id, archived_at=archived_at, path=archive_path))
    return sorted(archives, key=lambda archive: archive.archived_at, reverse=True)


def remove_canonical_env_and_archives(paths: InstallationPaths, agent_id: AgentId) -> None:
    """Delete the workspace's canonical env and every archived one.

    For a workspace whose backups are gone (reaped, evicted, or cleaned up as
    an orphan): the archives name previous destinations that are no longer
    worth listing, and hold the credentials of a storage nobody restores from.
    """
    canonical_env_path(paths, agent_id).unlink(missing_ok=True)
    for archive in list_archived_envs(paths, agent_id):
        archive.path.unlink(missing_ok=True)


def read_archived_env(paths: InstallationPaths, agent_id: AgentId, archive_id: str) -> str | None:
    """The contents of one archived canonical env, or None when no such archive exists.

    ``archive_id`` must be a bare archive timestamp; anything else (a path, a
    malformed id) answers None rather than reaching outside the env directory.
    """
    if not _ENV_ARCHIVE_ID_RE.match(archive_id):
        return None
    archive_path = canonical_env_path(paths, agent_id).with_name(f"{agent_id}.env.{archive_id}")
    if not archive_path.is_file():
        return None
    try:
        return archive_path.read_text()
    except OSError as e:
        raise BackupProvisioningError(f"Could not read archived restic.env at {archive_path}: {e}") from e


def list_previous_destination_envs(paths: InstallationPaths, agent_id: AgentId) -> list[ArchivedBackupEnv]:
    """The archived envs naming a repository other than the current canonical env's, newest first.

    An archive of the current repository (backups turned off and on again, a
    destination change rolled back after ``restic init``) is not a previous
    destination: its token is gone and its snapshots are the current ones.
    """
    canonical = read_canonical_env(paths, agent_id)
    current_repository = _repository_key(parse_restic_env(canonical)) if canonical is not None else None
    previous: list[ArchivedBackupEnv] = []
    for archive in list_archived_envs(paths, agent_id):
        try:
            archived_repository = _repository_key(parse_restic_env(archive.path.read_text()))
        except OSError as e:
            logger.warning("Skipping unreadable archived restic.env {} for {}: {}", archive.path.name, agent_id, e)
            continue
        if archived_repository != current_repository:
            previous.append(archive)
    return previous


@pure
def _repository_key(env: dict[str, str]) -> str:
    """A ``RESTIC_REPOSITORY`` value normalized for equality (restic treats the trailing slash as optional)."""
    return env.get("RESTIC_REPOSITORY", "").strip().rstrip("/")


def archived_password_for_repository(paths: InstallationPaths, agent_id: AgentId, repository: str) -> str | None:
    """The ``RESTIC_PASSWORD`` the newest archived env naming ``repository`` holds, or None.

    A repository is keyed once, by the password of the env that initialized
    it; an archived env naming the same repository (a destination change
    rolled back after ``restic init``, or backups turned off and on again)
    is the only place that password survives, so re-provisioning the same
    repository must reuse it rather than key a new one it cannot open.
    """
    wanted = _repository_key({"RESTIC_REPOSITORY": repository})
    for archive in list_archived_envs(paths, agent_id):
        try:
            env = parse_restic_env(archive.path.read_text())
        except OSError as e:
            logger.warning("Skipping unreadable archived restic.env {} for {}: {}", archive.path.name, agent_id, e)
            continue
        if _repository_key(env) == wanted and env.get("RESTIC_PASSWORD"):
            return env["RESTIC_PASSWORD"]
    return None


# The R2 endpoint marker identifying a legacy (per-bucket S3) imbue_cloud repository.
# CLEANUP: remove the legacy bucket parsing once the backup-store migration's
# legacy reap has run on production and no canonical env names an S3 bucket.
_R2_ENDPOINT_MARKER: Final[str] = ".r2.cloudflarestorage.com"
# A backup-store repository: restic's REST backend on the per-environment
# Worker, at ``<hyphen-stripped user id>/<workspace id>/``. The two trailing
# path segments are what distinguish it from a user's own rest-server.
_BACKUP_STORE_REPOSITORY_RE: Final[re.Pattern[str]] = re.compile(
    r"^rest:https?://[^/]+/(?P<user>[0-9a-f]{32})/(?P<workspace>agent-[0-9a-f]+)/?$"
)


class BackupStoreRepository(FrozenModel):
    """The coordinates of a backup-store repository parsed from its restic REST URL."""

    user_segment: str = Field(description="The owner's SuperTokens user id with hyphens stripped")
    workspace_id: str = Field(description="The workspace id the repository belongs to")

    @property
    def path(self) -> str:
        """The ``<user>/<workspace>`` repository path (the record's backup_bucket value)."""
        return f"{self.user_segment}/{self.workspace_id}"


@pure
def parse_backup_store_repository(repository: str) -> BackupStoreRepository | None:
    """Parse a ``RESTIC_REPOSITORY`` value into backup-store coordinates, or None for any other backend."""
    match = _BACKUP_STORE_REPOSITORY_RE.match(repository.strip())
    if match is None:
        return None
    return BackupStoreRepository(user_segment=match.group("user"), workspace_id=match.group("workspace"))


@pure
def backup_store_repository_from_env(env_content: str) -> BackupStoreRepository | None:
    """The backup-store coordinates of a canonical env, or None for a legacy bucket or BYO backend."""
    return parse_backup_store_repository(parse_restic_env(env_content).get("RESTIC_REPOSITORY", ""))


@pure
def is_legacy_bucket_repository(repository: str) -> bool:
    """Whether a ``RESTIC_REPOSITORY`` value is a legacy per-workspace R2 bucket over S3."""
    return repository.startswith("s3:") and _R2_ENDPOINT_MARKER in repository


@pure
def split_backup_bucket_name_from_env(env_content: str) -> tuple[str, str] | None:
    """``(owner_prefix, short_name)`` of a legacy backup bucket from a canonical env, else None.

    Legacy imbue_cloud repositories look like
    ``s3:https://<acct>.r2.cloudflarestorage.com/<owner-prefix>--<short-name>``;
    a backup-store repository or a bring-your-own backend answers None.
    """
    repository = parse_restic_env(env_content).get("RESTIC_REPOSITORY", "")
    if not is_legacy_bucket_repository(repository):
        return None
    bucket_name = repository.rstrip("/").rsplit("/", 1)[-1]
    if "--" not in bucket_name:
        return None
    owner_prefix, _, short_name = bucket_name.partition("--")
    return owner_prefix, short_name


@pure
def full_backup_bucket_name_from_env(env_content: str) -> str | None:
    """The full R2 bucket name of a legacy backup bucket from a canonical env, else None."""
    parsed = split_backup_bucket_name_from_env(env_content)
    if parsed is None:
        return None
    owner_prefix, short_name = parsed
    return f"{owner_prefix}--{short_name}"


@pure
def backup_location_from_env(env_content: str) -> str | None:
    """The workspace record's ``backup_bucket`` value for a canonical env, or None for BYO backends.

    A backup-store repository is recorded as its ``<user>/<workspace>`` path;
    a legacy bucket as its full R2 name. Both reapers accept either shape.
    """
    store = backup_store_repository_from_env(env_content)
    if store is not None:
        return store.path
    return full_backup_bucket_name_from_env(env_content)


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
