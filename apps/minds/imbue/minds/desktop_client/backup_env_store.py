"""Canonical per-workspace restic env files, owned by the minds app.

minds is the source of truth for how to reach each workspace's restic
repository. For every workspace with backups configured, minds keeps the
definitive ``restic.env`` (repository URL + backend credentials + the
workspace's random ``RESTIC_PASSWORD``) here, 0600, under the minds env's
data dir. The copy inside the workspace at ``runtime/secrets/restic.env``
is just an injected mirror of this file; config changes are made here and
re-injected whole.

These files are never auto-deleted -- not even on workspace destroy -- so a
stopped or destroyed workspace's backups stay reachable for status checks
and restores.
"""

import os
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_BACKUP_ENV_DIRNAME = "backup_envs"


def backup_env_dir(paths: WorkspacePaths) -> Path:
    """Return the directory holding the canonical per-workspace restic env files."""
    return paths.data_dir / _BACKUP_ENV_DIRNAME


def canonical_env_path(paths: WorkspacePaths, agent_id: AgentId) -> Path:
    """Return the path of the canonical restic.env for ``agent_id``."""
    return backup_env_dir(paths) / f"{agent_id}.env"


def has_canonical_env(paths: WorkspacePaths, agent_id: AgentId) -> bool:
    """Return whether a canonical restic.env exists for ``agent_id``."""
    return canonical_env_path(paths, agent_id).is_file()


def read_canonical_env(paths: WorkspacePaths, agent_id: AgentId) -> str | None:
    """Return the canonical restic.env contents for ``agent_id``, or None if absent."""
    path = canonical_env_path(paths, agent_id)
    if not path.is_file():
        return None
    try:
        return path.read_text()
    except OSError as e:
        raise BackupProvisioningError(f"Could not read canonical restic.env at {path}: {e}") from e


def write_canonical_env(paths: WorkspacePaths, agent_id: AgentId, content: str) -> None:
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
