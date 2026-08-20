"""Pytest-facing utilities for the deployment_tests suite.

Separate from ``helpers.py`` because these call into ``pytest`` (skip/fail),
and ``helpers.py`` ships in the built wheel where pytest is not a dependency;
``testing.py`` modules are excluded from the wheel by convention.
"""

import os
from typing import Final
from typing import NoReturn

import pytest

from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import PoolProvisionInfo

# Opt-out for envs that legitimately have no pool capacity (e.g. an operator's
# dev env without a baked box): restores the old skip-on-503 behavior. The CI
# release flow never sets it -- there, an empty pool means the bake stage broke
# and must FAIL the tests rather than skip them green.
POOL_ALLOW_EMPTY_ENV_VAR: Final[str] = "MINDS_ALLOW_EMPTY_POOL"


def handle_no_pool_capacity(reason: str) -> NoReturn:
    """React to a no-capacity lease (503): fail by default, skip only on explicit opt-out.

    See specs/remote-workspaces-in-ci.md: once the CI bake stage guarantees pool
    capacity, an empty pool silently skipping these tests green is the worst
    outcome, so the default is a hard failure.
    """
    if os.environ.get(POOL_ALLOW_EMPTY_ENV_VAR) == "1":
        pytest.skip(f"{reason} ({POOL_ALLOW_EMPTY_ENV_VAR}=1 allows envs without pool capacity)")
    pytest.fail(
        f"{reason}. Pool capacity is required by default: the bake stage should have pre-provisioned "
        f"slices (specs/remote-workspaces-in-ci.md). Set {POOL_ALLOW_EMPTY_ENV_VAR}=1 only for envs "
        "that legitimately have no pool."
    )


def require_pool_info(config: DeploymentEnvsConfig) -> PoolProvisionInfo:
    """The run's pre-baked pool info, with the same required-by-default semantics as leasing."""
    if config.pool is None:
        handle_no_pool_capacity("This run has no pre-baked pool (deployment_envs.json carries no pool info)")
    return config.pool
