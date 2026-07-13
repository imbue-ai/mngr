"""Unit tests for the master-password rotation (real restic against local repos)."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_password_rotation import rotate_backup_master_password
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password
from imbue.minds.desktop_client.backup_password_store import verify_backup_password
from imbue.minds.desktop_client.backup_password_store import write_backup_password_hash
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

_WORKSPACE_PASSWORD = "workspace-random-password"
_OLD_MASTER = "old-master-password"


def _paths(tmp_path: Path) -> WorkspacePaths:
    data_dir = tmp_path / "minds-data"
    data_dir.mkdir(exist_ok=True)
    return WorkspacePaths(data_dir=data_dir)


def _provision_workspace_repo(tmp_path: Path, paths: WorkspacePaths, agent_id: AgentId, *, name: str) -> str:
    """Init a local repo keyed like provisioning does (master key + workspace key)."""
    repository = str(tmp_path / name)
    restic_cli.init_repo(repository=repository, backend_env={}, password=_OLD_MASTER)
    restic_cli.add_password_key(
        repository=repository,
        backend_env={},
        existing_password=_OLD_MASTER,
        new_password=_WORKSPACE_PASSWORD,
    )
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD={_WORKSPACE_PASSWORD}\n")
    return repository


@pytest.mark.timeout(120)
def test_rotation_rekeys_the_repo_and_updates_the_hash(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    repository = _provision_workspace_repo(tmp_path, paths, agent_id, name="repo-one")
    write_backup_password_hash(paths, SecretStr(_OLD_MASTER))
    save_backup_password(paths, SecretStr(_OLD_MASTER))
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    result = rotate_backup_master_password(
        paths=paths,
        resolver=resolver,
        new_password=SecretStr("new-master-password"),
        is_save_password=False,
    )

    assert result.is_all_ok is True
    assert [entry.agent_id for entry in result.results] == [str(agent_id)]
    # The repo ends in the clean two-key state: workspace key + new master.
    keys = restic_cli.list_keys(repository=repository, backend_env={}, password=_WORKSPACE_PASSWORD)
    assert len(keys) == 2
    # The new master opens the repo; the old one no longer does.
    assert restic_cli.list_keys(repository=repository, backend_env={}, password="new-master-password")
    with pytest.raises(BackupProvisioningError):
        restic_cli.list_keys(repository=repository, backend_env={}, password=_OLD_MASTER)
    # The hash is the new password's, and the stale plaintext copy is gone.
    assert verify_backup_password(paths, SecretStr("new-master-password")) is True
    assert verify_backup_password(paths, SecretStr(_OLD_MASTER)) is False
    assert read_saved_backup_password(paths) is None


@pytest.mark.timeout(120)
def test_rotation_to_the_empty_password_works(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    repository = _provision_workspace_repo(tmp_path, paths, agent_id, name="repo-empty")
    write_backup_password_hash(paths, SecretStr(_OLD_MASTER))
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    result = rotate_backup_master_password(
        paths=paths, resolver=resolver, new_password=SecretStr(""), is_save_password=False
    )

    assert result.is_all_ok is True
    # The empty-password key opens the repo (restic's --insecure-no-password path).
    assert restic_cli.list_keys(repository=repository, backend_env={}, password=None)
    assert verify_backup_password(paths, SecretStr("")) is True


@pytest.mark.timeout(120)
def test_rotation_saves_the_new_password_when_asked(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    _provision_workspace_repo(tmp_path, paths, agent_id, name="repo-save")
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    rotate_backup_master_password(
        paths=paths, resolver=resolver, new_password=SecretStr("brand-new"), is_save_password=True
    )

    assert read_saved_backup_password(paths) == "brand-new"


@pytest.mark.timeout(120)
def test_rotation_skips_workspaces_without_backups(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    result = rotate_backup_master_password(
        paths=paths, resolver=resolver, new_password=SecretStr("whatever"), is_save_password=False
    )

    assert result.results == ()
    assert result.is_all_ok is True
    assert verify_backup_password(paths, SecretStr("whatever")) is True


@pytest.mark.timeout(120)
def test_rotation_reports_per_workspace_failures_and_still_updates_the_hash(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    # A canonical env pointing at a repository that does not exist.
    write_canonical_env(
        paths, agent_id, f"RESTIC_REPOSITORY={tmp_path / 'missing-repo'}\nRESTIC_PASSWORD=irrelevant\n"
    )
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    result = rotate_backup_master_password(
        paths=paths, resolver=resolver, new_password=SecretStr("intended"), is_save_password=False
    )

    assert result.is_all_ok is False
    assert len(result.results) == 1
    assert result.results[0].is_ok is False
    assert result.results[0].error
    # The user's intent stands: the hash moved to the new password so a re-run
    # (retyping the same new password) can retry the failed repository.
    assert verify_backup_password(paths, SecretStr("intended")) is True


@pytest.mark.timeout(120)
def test_rotation_is_idempotent_across_reruns(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId()
    repository = _provision_workspace_repo(tmp_path, paths, agent_id, name="repo-rerun")
    resolver = make_resolver_with_data(make_agents_json(agent_id))

    for _ in range(2):
        result = rotate_backup_master_password(
            paths=paths, resolver=resolver, new_password=SecretStr("same-new"), is_save_password=False
        )
        assert result.is_all_ok is True

    keys = restic_cli.list_keys(repository=repository, backend_env={}, password=_WORKSPACE_PASSWORD)
    assert len(keys) == 2
    assert restic_cli.list_keys(repository=repository, backend_env={}, password="same-new")
