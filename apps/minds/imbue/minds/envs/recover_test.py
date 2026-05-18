"""Unit tests for the recover-target file IO + reversal logic."""

from pathlib import Path

import pytest

from imbue.minds.envs.recover import NotInMonorepoError
from imbue.minds.envs.recover import RecoverTarget
from imbue.minds.envs.recover import RecoverTargetAlreadyExistsError
from imbue.minds.envs.recover import RecoverTargetMissingError
from imbue.minds.envs.recover import delete_recover_target
from imbue.minds.envs.recover import find_all_recover_target_files
from imbue.minds.envs.recover import find_monorepo_root
from imbue.minds.envs.recover import make_neon_snapshot_branch_name
from imbue.minds.envs.recover import read_recover_target
from imbue.minds.envs.recover import recover_target_exists
from imbue.minds.envs.recover import recover_target_path
from imbue.minds.envs.recover import write_recover_target_atomic
from imbue.minds.envs.secret_lifecycle import DeployId

_ENV_NAME = "dev-josh-1"


def _sample_target(env_name: str = _ENV_NAME) -> RecoverTarget:
    return RecoverTarget(
        deploy_id=DeployId("20260517T143022Z"),
        env_name=env_name,
        tier="dev",
        modal_env=env_name,
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
    # Per-env naming: the file is named after the target's env_name.
    assert written == tmp_path / f".minds-deploy-recover-target-{_ENV_NAME}.json"
    assert recover_target_exists(repo_root=tmp_path, env_name=_ENV_NAME)
    assert read_recover_target(repo_root=tmp_path, env_name=_ENV_NAME) == target


def test_write_recover_target_refuses_if_exists(tmp_path: Path) -> None:
    write_recover_target_atomic(_sample_target(), repo_root=tmp_path)
    with pytest.raises(RecoverTargetAlreadyExistsError):
        write_recover_target_atomic(_sample_target(), repo_root=tmp_path)


def test_read_recover_target_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(RecoverTargetMissingError):
        read_recover_target(repo_root=tmp_path, env_name=_ENV_NAME)


def test_delete_recover_target_is_idempotent(tmp_path: Path) -> None:
    # No file: should not raise.
    delete_recover_target(repo_root=tmp_path, env_name=_ENV_NAME)
    # With file: deletes.
    write_recover_target_atomic(_sample_target(), repo_root=tmp_path)
    delete_recover_target(repo_root=tmp_path, env_name=_ENV_NAME)
    assert not recover_target_exists(repo_root=tmp_path, env_name=_ENV_NAME)


def test_recover_target_path_is_at_monorepo_root(tmp_path: Path) -> None:
    expected = tmp_path / f".minds-deploy-recover-target-{_ENV_NAME}.json"
    assert recover_target_path(repo_root=tmp_path, env_name=_ENV_NAME) == expected


def test_find_all_recover_target_files_returns_sorted_per_env_files(tmp_path: Path) -> None:
    """Per-env files are independently writable; the discover helper lists every one."""
    write_recover_target_atomic(_sample_target(env_name="dev-alice"), repo_root=tmp_path)
    write_recover_target_atomic(_sample_target(env_name="dev-bob"), repo_root=tmp_path)
    found = find_all_recover_target_files(repo_root=tmp_path)
    assert [p.name for p in found] == [
        ".minds-deploy-recover-target-dev-alice.json",
        ".minds-deploy-recover-target-dev-bob.json",
    ]


def test_per_env_recover_target_files_are_independent(tmp_path: Path) -> None:
    """A recover-target for env A does NOT make env B's existence check fire.

    This is the F26 invariant that supports test parallelism: each test
    can deploy its own random-named env without tripping over peers.
    """
    write_recover_target_atomic(_sample_target(env_name="dev-alice"), repo_root=tmp_path)
    assert recover_target_exists(repo_root=tmp_path, env_name="dev-alice")
    assert not recover_target_exists(repo_root=tmp_path, env_name="dev-bob")


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
    parsed = read_recover_target(repo_root=tmp_path, env_name=_ENV_NAME)
    assert parsed.app_versions_to_restore["llm-dev"] is None
    assert parsed.app_versions_to_restore["rsc-dev"] == "v17"
