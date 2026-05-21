"""Unit tests for the env-name -> tier mapping shared by minds CLI subcommands."""

from imbue.minds.cli._activated_env import CI_TIER
from imbue.minds.cli._activated_env import DEV_TIER
from imbue.minds.cli._activated_env import PRODUCTION_ENV_NAME
from imbue.minds.cli._activated_env import STAGING_ENV_NAME
from imbue.minds.cli._activated_env import tier_for_env_name


def test_tier_for_env_name_production() -> None:
    assert tier_for_env_name(PRODUCTION_ENV_NAME) == PRODUCTION_ENV_NAME


def test_tier_for_env_name_staging() -> None:
    assert tier_for_env_name(STAGING_ENV_NAME) == STAGING_ENV_NAME


def test_tier_for_env_name_dev_env_returns_dev() -> None:
    assert tier_for_env_name("dev-josh") == DEV_TIER
    assert tier_for_env_name("dev-alice-3") == DEV_TIER


def test_tier_for_env_name_ci_env_returns_ci() -> None:
    """Ephemeral CI envs minted by the deployment-tests orchestrator route to the ci tier."""
    assert tier_for_env_name("ci-20260518t140212z") == CI_TIER
    assert tier_for_env_name("ci-20260518t140212z-abcd") == CI_TIER


def test_tier_for_env_name_dev_prefixed_with_ci_substring_still_dev() -> None:
    """A dev-prefixed env whose user portion happens to contain 'ci' is still dev tier.

    Regression guard: the ci check is a prefix match on ``ci-``, not a
    substring search, so a name like ``dev-ci-leftover`` (an artifact of
    the old ``dev-ci-`` naming convention) must still route to dev.
    """
    assert tier_for_env_name("dev-ci-leftover") == DEV_TIER
