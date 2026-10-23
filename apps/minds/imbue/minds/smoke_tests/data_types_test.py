import pytest

from imbue.minds.deployment_tests.primitives import MAILTM_ADDRESS_ENV_VAR
from imbue.minds.deployment_tests.primitives import MAILTM_JWT_ENV_VAR
from imbue.minds.smoke_tests.data_types import HANDOFF_ENV_VARS
from imbue.minds.smoke_tests.data_types import HANDOFF_VAULT_KEYS
from imbue.minds.smoke_tests.data_types import SmokeRunHandoff
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY


def test_a_handoff_round_trips_through_env_vars(smoke_run_handoff: SmokeRunHandoff) -> None:
    assert SmokeRunHandoff.from_env_or_none(smoke_run_handoff.to_env()) == smoke_run_handoff


def test_a_handoff_round_trips_through_its_state_file_json(smoke_run_handoff: SmokeRunHandoff) -> None:
    assert SmokeRunHandoff.from_state_json(smoke_run_handoff.to_state_json()) == smoke_run_handoff


def test_a_handoff_round_trips_through_its_vault_leaves(smoke_run_handoff: SmokeRunHandoff) -> None:
    rebuilt = SmokeRunHandoff.from_vault_values(
        smoke_run_handoff.to_vault_values(),
        run_id=smoke_run_handoff.run_id,
        tier=str(smoke_run_handoff.tier),
    )
    assert rebuilt == smoke_run_handoff


def test_every_representation_carries_the_secrets_its_consumer_needs(smoke_run_handoff: SmokeRunHandoff) -> None:
    environment = smoke_run_handoff.to_env()
    assert set(environment) == set(HANDOFF_ENV_VARS)
    assert set(smoke_run_handoff.to_vault_values()) == set(HANDOFF_VAULT_KEYS)
    assert environment[SMOKE_THROWAWAY_PASSWORD_ENV_VAR] == "Smoke-throwaway-1"
    assert environment[MAILTM_ADDRESS_ENV_VAR] == str(smoke_run_handoff.mailtm_address)
    assert environment[MAILTM_JWT_ENV_VAR] == "mailtm-jwt-value"
    assert "Smoke-throwaway-1" in smoke_run_handoff.to_state_json()


def test_an_environment_carrying_no_handoff_reads_as_none() -> None:
    assert SmokeRunHandoff.from_env_or_none({"PATH": "/usr/bin"}) is None


def test_an_environment_naming_only_the_tier_carries_no_handoff() -> None:
    assert SmokeRunHandoff.from_env_or_none({SMOKE_TIER_ENV_VAR: "staging"}) is None


def test_a_half_populated_environment_is_refused_rather_than_half_read(
    smoke_run_handoff: SmokeRunHandoff,
) -> None:
    partial = smoke_run_handoff.to_env()
    del partial[SMOKE_THROWAWAY_PASSWORD_ENV_VAR]
    with pytest.raises(SmokeConfigError, match=SMOKE_THROWAWAY_PASSWORD_ENV_VAR):
        SmokeRunHandoff.from_env_or_none(partial)


def test_a_half_populated_vault_handoff_is_refused_by_its_leaf_name(
    smoke_run_handoff: SmokeRunHandoff,
) -> None:
    leaves = smoke_run_handoff.to_vault_values()
    del leaves[THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY]
    with pytest.raises(SmokeConfigError, match=THROWAWAY_ACCOUNT_USER_ID_VAULT_KEY):
        SmokeRunHandoff.from_vault_values(leaves, run_id=smoke_run_handoff.run_id, tier=str(smoke_run_handoff.tier))


def test_a_state_file_that_is_not_json_is_refused() -> None:
    with pytest.raises(SmokeConfigError, match="not JSON"):
        SmokeRunHandoff.from_state_json("this is not json")


def test_a_state_file_holding_a_json_array_is_refused() -> None:
    with pytest.raises(SmokeConfigError, match="expected an object"):
        SmokeRunHandoff.from_state_json("[]")


def test_the_throwaway_account_signs_in_as_its_mailbox_address(smoke_run_handoff: SmokeRunHandoff) -> None:
    credentials = smoke_run_handoff.throwaway.as_credentials()
    assert str(credentials.email) == str(smoke_run_handoff.mailtm_address)
    assert credentials.password.get_secret_value() == "Smoke-throwaway-1"
