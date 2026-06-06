"""Unit tests for the per-env Modal Secret override computation.

The full deploy flow's behaviour is exercised by
``provisioning_test.py``; this file pins the contract that
:func:`compute_per_env_overrides` returns BOTH ``neon.DATABASE_URL`` and
``litellm.DATABASE_URL`` overrides (the former for the connector's
pool-host queries, the latter for the LiteLLM proxy's Prisma-managed
backing store). Both DSNs come from the same per-env Neon project.
"""

from pydantic import SecretStr

from imbue.minds.envs.per_env_deploy import compute_per_env_overrides
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.envs.providers.neon_db import NeonProjectRecord
from imbue.minds.envs.providers.supertokens_app import SuperTokensAppRecord


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
