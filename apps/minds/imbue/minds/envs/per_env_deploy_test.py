"""Unit tests for the per-env Modal Secret override computation.

The full deploy flow's behaviour is exercised by
``provisioning_test.py``; this file pins the contract that
:func:`compute_per_env_overrides` returns BOTH ``neon.DATABASE_URL`` and
``litellm.DATABASE_URL`` overrides (the former for the connector's
pool-host queries, the latter for the LiteLLM proxy's Prisma-managed
backing store). Both DSNs come from the same per-env Neon project.
"""

import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.per_env_deploy import ModalDeployError
from imbue.minds.envs.per_env_deploy import compute_per_env_overrides
from imbue.minds.envs.per_env_deploy import ensure_modal_env
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord


@pytest.fixture
def _root_cg() -> Iterator[ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="per-env-deploy-test-root")
    with cg:
        yield cg


def _make_fake_modal_binary(tmp_path: Path, *, exit_code: int, stderr: str = "") -> Path:
    stderr_path = tmp_path / "_fake_modal_stderr.txt"
    stderr_path.write_text(stderr)
    script = tmp_path / "modal"
    script.write_text(f"#!/usr/bin/env bash\ncat {stderr_path} >&2\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_ensure_modal_env_succeeds_on_zero_exit(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_modal_binary(tmp_path, exit_code=0)
    ensure_modal_env(DevEnvName("dev-josh"), parent_cg=_root_cg, modal_binary=str(fake))


def test_ensure_modal_env_tolerates_already_exists(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_modal_binary(tmp_path, exit_code=1, stderr="Environment 'dev-josh' already exists")
    ensure_modal_env(DevEnvName("dev-josh"), parent_cg=_root_cg, modal_binary=str(fake))


def test_ensure_modal_env_raises_on_does_not_exist(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    # The regression: a "does not exist" failure must NOT be swallowed by a
    # bare "exist" substring match (which would proceed to deploy against an
    # env that was never created).
    fake = _make_fake_modal_binary(tmp_path, exit_code=1, stderr="Workspace 'minds-dev' does not exist")
    with pytest.raises(ModalDeployError, match="does not exist"):
        ensure_modal_env(DevEnvName("dev-josh"), parent_cg=_root_cg, modal_binary=str(fake))


def _fake_neon_record() -> NeonProjectRecord:
    return NeonProjectRecord(
        project_id="proj-fake-123",
        project_name="minds-dev-josh",
        branch_id="branch-1",
        host_pool_dsn=SecretStr("postgresql://owner:pw@pooler/host_pool"),
        litellm_cost_dsn=SecretStr("postgresql://owner:pw@pooler/litellm_cost"),
    )


def _fake_supertokens_record() -> SuperTokensAppRecord:
    return SuperTokensAppRecord(
        app_id="dev-josh",
        connection_uri="https://core.example.com/appid-dev-josh",
        api_key=SecretStr("st-api-key"),
    )


def test_compute_per_env_overrides_includes_both_dsn_overrides() -> None:
    """Both neon and litellm services get DSN overrides from the per-env project."""
    overrides = compute_per_env_overrides(
        DevEnvName("dev-josh"),
        modal_workspace="minds-dev",
        tier="dev",
        neon_record=_fake_neon_record(),
        supertokens_record=_fake_supertokens_record(),
    )

    assert overrides["neon"] == {"DATABASE_URL": "postgresql://owner:pw@pooler/host_pool"}
    assert overrides["litellm"] == {"DATABASE_URL": "postgresql://owner:pw@pooler/litellm_cost"}


def test_compute_per_env_overrides_does_not_override_unrelated_services() -> None:
    """Services other than neon / litellm / supertokens / litellm-connector are untouched.

    The dev-tier deploy reads tier-shared values for everything else
    (``cloudflare``, ``pool-ssh``) straight from Vault. The override
    dict only exists for keys we genuinely need to rewrite at deploy
    time.
    """
    overrides = compute_per_env_overrides(
        DevEnvName("dev-josh"),
        modal_workspace="minds-dev",
        tier="dev",
        neon_record=_fake_neon_record(),
        supertokens_record=_fake_supertokens_record(),
    )
    assert set(overrides.keys()) == {"supertokens", "neon", "litellm", "litellm-connector"}
