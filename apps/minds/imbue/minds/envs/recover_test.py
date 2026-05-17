"""Unit tests for the recover-target file IO + reversal logic."""

from pathlib import Path

import pytest

from imbue.minds.envs.recover import NotInMonorepoError
from imbue.minds.envs.recover import RECOVER_TARGET_FILENAME
from imbue.minds.envs.recover import RecoverTarget
from imbue.minds.envs.recover import RecoverTargetAlreadyExistsError
from imbue.minds.envs.recover import RecoverTargetMissingError
from imbue.minds.envs.recover import delete_recover_target
from imbue.minds.envs.recover import find_monorepo_root
from imbue.minds.envs.recover import make_neon_snapshot_branch_name
from imbue.minds.envs.recover import read_recover_target
from imbue.minds.envs.recover import recover_target_exists
from imbue.minds.envs.recover import recover_target_path
from imbue.minds.envs.recover import write_recover_target_atomic
from imbue.minds.envs.secret_lifecycle import DeployId


def _sample_target() -> RecoverTarget:
    return RecoverTarget(
        deploy_id=DeployId("20260517T143022Z"),
        env_name="dev-josh-1",
        tier="dev",
        modal_env="dev-josh-1",
        modal_workspace="minds-dev",
        vault_path_prefix="secrets/minds/dev",
        neon_project_id="proj-fake-123",
        neon_branch_id="br-main-1",
        neon_snapshot_branch_id="br-snap-pre-deploy",
        app_versions_to_restore={"rsc-dev": "v17", "llm-dev": None},
    )


def test_recover_target_round_trip_via_json() -> None:
    target = _sample_target()
    raw = target.to_json_bytes()
    parsed = RecoverTarget.from_json_bytes(raw)
    assert parsed == target


def test_write_recover_target_atomic_creates_file(tmp_path: Path) -> None:
    target = _sample_target()
    written = write_recover_target_atomic(target, repo_root=tmp_path)
    assert written == tmp_path / RECOVER_TARGET_FILENAME
    assert recover_target_exists(repo_root=tmp_path)
    assert read_recover_target(repo_root=tmp_path) == target


def test_write_recover_target_refuses_if_exists(tmp_path: Path) -> None:
    write_recover_target_atomic(_sample_target(), repo_root=tmp_path)
    with pytest.raises(RecoverTargetAlreadyExistsError):
        write_recover_target_atomic(_sample_target(), repo_root=tmp_path)


def test_read_recover_target_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(RecoverTargetMissingError):
        read_recover_target(repo_root=tmp_path)


def test_delete_recover_target_is_idempotent(tmp_path: Path) -> None:
    # No file: should not raise.
    delete_recover_target(repo_root=tmp_path)
    # With file: deletes.
    write_recover_target_atomic(_sample_target(), repo_root=tmp_path)
    delete_recover_target(repo_root=tmp_path)
    assert not recover_target_exists(repo_root=tmp_path)


def test_recover_target_path_is_at_monorepo_root(tmp_path: Path) -> None:
    expected = tmp_path / RECOVER_TARGET_FILENAME
    assert recover_target_path(repo_root=tmp_path) == expected


def test_find_monorepo_root_walks_up_to_apps_marker(tmp_path: Path) -> None:
    (tmp_path / "apps").mkdir()
    (tmp_path / "nested" / "deeper").mkdir(parents=True)
    assert find_monorepo_root(cwd=tmp_path / "nested" / "deeper") == tmp_path


def test_find_monorepo_root_raises_when_no_marker(tmp_path: Path) -> None:
    with pytest.raises(NotInMonorepoError):
        find_monorepo_root(cwd=tmp_path)


def test_make_neon_snapshot_branch_name() -> None:
    assert make_neon_snapshot_branch_name(DeployId("20260517T143022Z")) == "pre-deploy-20260517T143022Z"


def test_app_versions_to_restore_may_carry_none(tmp_path: Path) -> None:
    """First-ever deploy leaves the captured version as None for skip-with-warning."""
    target = _sample_target()
    write_recover_target_atomic(target, repo_root=tmp_path)
    parsed = read_recover_target(repo_root=tmp_path)
    assert parsed.app_versions_to_restore["llm-dev"] is None
    assert parsed.app_versions_to_restore["rsc-dev"] == "v17"
