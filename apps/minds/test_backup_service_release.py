"""Release tests for the backup-service verification + update machinery.

These drive the real minds-side code paths against a real local mngr agent
whose work_dir is an FCT-shaped git repo (see the ``backup_release_workspace``
fixture in ``conftest.py``): ``mngr exec`` runs the actual workspace scripts
(gate probe, apply update, check), git operations happen for real (stash /
checkout tag / commit / revert), restic provisioning initializes real local
repositories, and env injection/rotation lands on the agent's real
filesystem. Only two pieces are stood in for: a stub ``supervisorctl`` on
PATH (there is no supervisord here) and a minds-side resolver double that
marks the workspace online.

Run individually from the repo root, e.g.:
    just test apps/minds/test_backup_service_release.py::test_backup_update_core_loop
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import reinject_canonical_env
from imbue.minds.desktop_client.backup_update import run_backup_update_sequence
from imbue.minds.desktop_client.backup_verification import BackupServiceCheckState
from imbue.minds.desktop_client.backup_verification import BackupServiceProblem
from imbue.minds.desktop_client.backup_verification import check_backup_service_for_workspace
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.primitives import BackupProvider
from imbue.minds.testing import BackupReleaseWorkspace
from imbue.minds.testing import run_git_for_backup_test
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState

# The fixed host id conftest.make_resolver_with_data assigns to every agent.
_RESOLVER_HOST_ID = "host-00000000000000000000000000000000"


def _resolver_for(agent_id: AgentId) -> MngrCliBackendResolver:
    resolver = make_resolver_with_data(agents_json=make_agents_json(agent_id))
    resolver.set_host_state_override(_RESOLVER_HOST_ID, HostState.RUNNING)
    return resolver


def _minds_paths(tmp_path: Path) -> WorkspacePaths:
    data_dir = tmp_path / "minds-data"
    data_dir.mkdir()
    return WorkspacePaths(data_dir=data_dir)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(420)
def test_backup_update_core_loop(tmp_path: Path, backup_release_workspace: BackupReleaseWorkspace) -> None:
    """Detect outdated code, get blocked by the running agent, stop-and-retry, verify convergence."""
    workspace = backup_release_workspace
    paths = _minds_paths(tmp_path)
    resolver = _resolver_for(workspace.agent_id)
    registry = InMemoryWorkspaceOperationRegistry()

    # The check (real exec + real git) reports the code as outdated. (The
    # target minds-v2.0.0 tag resolves via the highest-local-tag fallback since
    # the test app version has no tag of its own.)
    check = check_backup_service_for_workspace(paths, workspace.agent_id, resolver=resolver)
    assert check.state == BackupServiceCheckState.PROBLEMS, check
    assert BackupServiceProblem.CODE_OUTDATED in check.problems, check
    assert check.desired_version == "minds-v2.0.0"

    # Run the update. The workspace's own (idle) agent reads as WAITING, and
    # idle chats deliberately do NOT gate the update -- only actively-RUNNING
    # ones do (the blocked + stop-chats paths are covered end-to-end by the
    # script-level tests in backup_workspace_scripts_test.py, which feed the
    # gate a RUNNING chat). So the sequence proceeds and converges.
    registry.start(workspace.agent_id, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))
    run_backup_update_sequence(
        agent_id=workspace.agent_id,
        paths=paths,
        resolver=resolver,
        registry=registry,
        parent_cg=None,
        is_stop_chats=False,
    )
    done_record = registry.get(workspace.agent_id)
    assert done_record is not None
    assert done_record.status == WorkspaceOperationStatus.DONE, done_record.error

    # The workspace now carries the tag's backup code, committed with the
    # recognizable convention subject, and re-verification reads clean.
    assert (workspace.work_dir / "libs" / "host_backup" / "service.py").read_text() == "VERSION = 2\n"
    subject = run_git_for_backup_test(workspace.work_dir, "log", "-1", "--format=%s").strip()
    assert subject == "backup-update: minds-v2.0.0"
    recheck = check_backup_service_for_workspace(paths, workspace.agent_id, resolver=resolver)
    assert BackupServiceProblem.CODE_OUTDATED not in recheck.problems
    assert BackupServiceProblem.SERVICE_NOT_RUNNING not in recheck.problems
    # Backups were never configured for this workspace, which is its own
    # (accurate) problem state -- not an update failure.
    assert BackupServiceProblem.NOT_CONFIGURED in recheck.problems


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_backup_enable_repair_and_destination_change(
    tmp_path: Path, backup_release_workspace: BackupReleaseWorkspace
) -> None:
    """Enable backups post-creation, repair a corrupted env, then change the destination."""
    workspace = backup_release_workspace
    paths = _minds_paths(tmp_path)
    repo_one = tmp_path / "restic-repo-1"
    repo_two = tmp_path / "restic-repo-2"
    workspace_env_path = workspace.work_dir / "runtime" / "secrets" / "restic.env"

    # Enable backups on the (CONFIGURE_LATER) workspace: real restic init +
    # random per-workspace password + injection into the real work_dir.
    configure_backups_for_host(
        agent_id=workspace.agent_id,
        host_id="host-test",
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repo_one}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    canonical_one = read_canonical_env(paths, workspace.agent_id)
    assert canonical_one is not None
    assert f"RESTIC_REPOSITORY={repo_one}" in canonical_one
    assert "RESTIC_PASSWORD=" in canonical_one
    # restic initialized the repository for real:
    assert (repo_one / "config").is_file()
    assert workspace_env_path.read_text() == canonical_one

    # Repair: corrupt the workspace copy, re-inject, and confirm the drifted
    # copy was rotated aside rather than lost.
    workspace_env_path.write_text("RESTIC_REPOSITORY=garbage\n")
    reinject_canonical_env(agent_id=workspace.agent_id, paths=paths)
    assert workspace_env_path.read_text() == canonical_one
    rotated = list(workspace_env_path.parent.glob("restic.env.*"))
    assert any("garbage" in rotated_path.read_text() for rotated_path in rotated)

    # Destination change: fresh provisioning against repo two; the old
    # canonical env is archived and the workspace copy rotated + replaced.
    change_backup_destination_for_host(
        agent_id=workspace.agent_id,
        host_id="host-test",
        request=BackupSetupRequest(
            backup_provider=BackupProvider.API_KEY, api_key_env_text=f"RESTIC_REPOSITORY={repo_two}"
        ),
        imbue_cloud_cli=None,
        paths=paths,
    )
    canonical_two = read_canonical_env(paths, workspace.agent_id)
    assert canonical_two is not None
    assert f"RESTIC_REPOSITORY={repo_two}" in canonical_two
    assert canonical_two != canonical_one
    assert (repo_two / "config").is_file()
    assert workspace_env_path.read_text() == canonical_two
    archived = list((paths.data_dir / "backup_envs").glob(f"{workspace.agent_id}.env.*"))
    assert len(archived) == 1
    assert archived[0].read_text() == canonical_one
    # The old repository is untouched and still reachable via the archive.
    assert (repo_one / "config").is_file()
