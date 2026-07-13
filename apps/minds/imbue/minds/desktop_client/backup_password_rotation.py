"""Change the shared backup master password across all existing workspaces.

The master password is only ever a *recovery* key: every workspace repository
also carries its own random ``RESTIC_PASSWORD`` (in the canonical env), which
is what authenticates the rotation. Per workspace whose host still exists and
that has a canonical env, the rotation:

1. ``restic key add``s the new master password (authenticated with the
   workspace's random password),
2. removes every other key, so the repository ends in a clean two-key state
   (the workspace's own key + the new master key).

Destroyed workspaces are skipped: their repositories stay reachable under the
old password via their (never-deleted) canonical envs. Failures are collected
per workspace -- the flow is synchronous, idempotent, and safely re-runnable
(rerunning converges every repository to the same two-key state).

After the per-repository work, the ``backup_password_hash`` becomes the hash
of the new password (this is the only flow allowed to change it), a stale
plaintext convenience copy is deleted, and -- when requested -- the new value
is saved as the fresh convenience copy.
"""

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_password_store import delete_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password
from imbue.minds.desktop_client.backup_password_store import write_backup_password_hash
from imbue.minds.errors import BackupProvisioningError


class WorkspaceRotationResult(FrozenModel):
    """The outcome of rekeying one workspace's repository."""

    agent_id: str = Field(description="The workspace agent id")
    workspace_name: str = Field(description="The workspace's display name (for the results list)")
    is_ok: bool = Field(description="Whether the repository now carries the new master key (and only it + its own)")
    error: str | None = Field(default=None, description="Why the rekey failed, when it did")


class BackupPasswordRotationResult(FrozenModel):
    """The outcome of a whole master-password rotation."""

    results: tuple[WorkspaceRotationResult, ...] = Field(description="One entry per existing workspace with backups")
    is_all_ok: bool = Field(description="Whether every workspace rekeyed cleanly")


def _rekey_repository_to_new_master(
    *,
    canonical_env: str,
    new_password: SecretStr,
    parent_cg: ConcurrencyGroup | None,
) -> None:
    """Add the new master key and strip the repository down to two keys.

    Authenticated entirely with the workspace's own random password, so the
    old master password is never needed. Raises ``BackupProvisioningError``
    on any restic failure.
    """
    env = parse_restic_env(canonical_env)
    repository = env.get("RESTIC_REPOSITORY", "")
    workspace_password = env.get("RESTIC_PASSWORD", "")
    if not repository or not workspace_password:
        raise BackupProvisioningError("The canonical restic.env lacks RESTIC_REPOSITORY/RESTIC_PASSWORD")
    backend_env = {key: value for key, value in env.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")}

    # Diffing the key listing around the add identifies the new key without
    # having to parse restic's human-readable add output.
    keys_before = restic_cli.list_keys(
        repository=repository, backend_env=backend_env, password=workspace_password, parent_cg=parent_cg
    )
    restic_cli.add_password_key(
        repository=repository,
        backend_env=backend_env,
        existing_password=workspace_password,
        new_password=new_password.get_secret_value(),
        parent_cg=parent_cg,
    )
    keys_after = restic_cli.list_keys(
        repository=repository, backend_env=backend_env, password=workspace_password, parent_cg=parent_cg
    )
    ids_before = {key.key_id for key in keys_before}
    added_ids = {key.key_id for key in keys_after if key.key_id not in ids_before}

    # Keep the workspace's own (current) key and the just-added master key;
    # everything else -- the old master key and any strays -- goes.
    for key in keys_after:
        if key.is_current or key.key_id in added_ids:
            continue
        restic_cli.remove_key(
            repository=repository,
            backend_env=backend_env,
            password=workspace_password,
            key_id=key.key_id,
            parent_cg=parent_cg,
        )


def rotate_backup_master_password(
    *,
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    new_password: SecretStr,
    is_save_password: bool,
    parent_cg: ConcurrencyGroup | None = None,
) -> BackupPasswordRotationResult:
    """Rekey every existing backed-up workspace to ``new_password`` and update the stores.

    The hash is updated even when some repositories failed (the user's intent
    stands; the per-workspace errors point at what to re-run). A stale
    plaintext convenience copy is deleted; ``is_save_password`` re-saves the
    new value instead.
    """
    results: list[WorkspaceRotationResult] = []
    with log_span("Rotating the backup master password"):
        for agent_id in resolver.list_active_workspace_ids():
            canonical_env = read_canonical_env(paths, agent_id)
            if canonical_env is None:
                continue
            workspace_name = resolver.get_workspace_name(agent_id) or str(agent_id)
            try:
                _rekey_repository_to_new_master(
                    canonical_env=canonical_env, new_password=new_password, parent_cg=parent_cg
                )
            except BackupProvisioningError as e:
                logger.warning("Rekeying the backup repository for {} failed: {}", agent_id, e)
                results.append(
                    WorkspaceRotationResult(
                        agent_id=str(agent_id), workspace_name=workspace_name, is_ok=False, error=str(e)
                    )
                )
                continue
            results.append(WorkspaceRotationResult(agent_id=str(agent_id), workspace_name=workspace_name, is_ok=True))

        write_backup_password_hash(paths, new_password)
        # The old plaintext copy no longer matches the hash; keep only a
        # freshly-saved copy of the new value when the user asked for one.
        delete_saved_backup_password(paths)
        if is_save_password and new_password.get_secret_value():
            save_backup_password(paths, new_password)
    return BackupPasswordRotationResult(results=tuple(results), is_all_ok=all(result.is_ok for result in results))
