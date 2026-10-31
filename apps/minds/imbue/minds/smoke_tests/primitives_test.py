import pytest

from imbue.minds.smoke_tests.primitives import PERSISTENT_ACCOUNT_EMAIL
from imbue.minds.smoke_tests.primitives import SMOKE_EMAIL_PREFIX
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.minds.smoke_tests.primitives import SmokeRunId
from imbue.minds.smoke_tests.primitives import persistent_account_vault_dir
from imbue.minds.smoke_tests.primitives import run_handoff_vault_dir


def test_smoke_run_id_accepts_a_timestamp_and_a_github_run_id() -> None:
    assert str(SmokeRunId("20261014t101500z")) == "20261014t101500z"
    assert str(SmokeRunId("18273645001")) == "18273645001"
    assert str(SmokeRunId("  20261014t101500z  ")) == "20261014t101500z"


@pytest.mark.parametrize(
    "value",
    [
        "20261014T101500Z",
        "run_17",
        "run 17",
        "run/17",
        "run.17",
        "",
    ],
)
def test_smoke_run_id_refuses_anything_a_vault_path_or_an_email_local_part_would_mangle(value: str) -> None:
    with pytest.raises(SmokeConfigError):
        SmokeRunId(value)


def test_persistent_account_vault_dir_is_the_tier_s_smoke_entry() -> None:
    assert str(persistent_account_vault_dir("production")) == "secrets/minds/production/smoke"
    assert str(persistent_account_vault_dir("staging")) == "secrets/minds/staging/smoke"


def test_run_handoff_vault_dir_is_per_run_and_under_the_ci_tier() -> None:
    first = run_handoff_vault_dir(SmokeRunId("run-1"))
    second = run_handoff_vault_dir(SmokeRunId("run-2"))
    assert str(first) == "secrets/minds/ci/smoke-runs/run-1"
    assert first != second


def test_the_persistent_account_is_itself_a_smoke_address() -> None:
    assert PERSISTENT_ACCOUNT_EMAIL.startswith(SMOKE_EMAIL_PREFIX)
