from pathlib import Path

import pytest

from imbue.minds.smoke_tests.data_types import SmokeRunHandoff
from imbue.minds.smoke_tests.handoff import read_handoff_state_file_or_none
from imbue.minds.smoke_tests.handoff import resolve_handoff_or_none
from imbue.minds.smoke_tests.handoff import resolve_test_job_handoff_or_none
from imbue.minds.smoke_tests.handoff import write_handoff_state_file
from imbue.minds.smoke_tests.primitives import SMOKE_HANDOFF_FILE_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_THROWAWAY_PASSWORD_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeRunId


def test_a_state_file_round_trips_the_handoff(tmp_path: Path, smoke_run_handoff: SmokeRunHandoff) -> None:
    state_path = tmp_path / "nested" / "smoke-run.json"
    write_handoff_state_file(smoke_run_handoff, state_path)
    assert read_handoff_state_file_or_none(state_path) == smoke_run_handoff


def test_a_state_file_is_readable_only_by_its_owner(tmp_path: Path, smoke_run_handoff: SmokeRunHandoff) -> None:
    state_path = tmp_path / "smoke-run.json"
    write_handoff_state_file(smoke_run_handoff, state_path)
    assert state_path.stat().st_mode & 0o077 == 0


def test_a_missing_state_file_reads_as_none(tmp_path: Path) -> None:
    assert read_handoff_state_file_or_none(tmp_path / "absent.json") is None


def test_the_environment_wins_over_a_state_file(
    tmp_path: Path, smoke_run_handoff: SmokeRunHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "smoke-run.json"
    write_handoff_state_file(smoke_run_handoff, state_path)
    exported_environment = {
        **smoke_run_handoff.to_env(),
        SMOKE_THROWAWAY_PASSWORD_ENV_VAR: "Smoke-from-the-environment-1",
    }
    for name, value in exported_environment.items():
        monkeypatch.setenv(name, value)

    resolved = resolve_handoff_or_none(
        run_id=smoke_run_handoff.run_id, tier=str(smoke_run_handoff.tier), state_path=state_path
    )

    assert resolved is not None
    assert resolved.to_env()[SMOKE_THROWAWAY_PASSWORD_ENV_VAR] == "Smoke-from-the-environment-1"


def test_a_state_file_is_read_when_the_environment_carries_no_run(
    tmp_path: Path, smoke_run_handoff: SmokeRunHandoff, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in smoke_run_handoff.to_env():
        monkeypatch.delenv(name, raising=False)
    state_path = tmp_path / "smoke-run.json"
    write_handoff_state_file(smoke_run_handoff, state_path)

    resolved = resolve_handoff_or_none(
        run_id=SmokeRunId("some-other-run"), tier=str(smoke_run_handoff.tier), state_path=state_path
    )

    assert resolved == smoke_run_handoff


def test_the_test_job_reads_the_handoff_the_environment_exports(smoke_run_handoff: SmokeRunHandoff) -> None:
    resolved = resolve_test_job_handoff_or_none(smoke_run_handoff.to_env(), tier=str(smoke_run_handoff.tier))
    assert resolved == smoke_run_handoff


def test_the_test_job_reads_the_handoff_file_a_local_run_names(
    tmp_path: Path, smoke_run_handoff: SmokeRunHandoff
) -> None:
    state_path = tmp_path / "smoke-run.json"
    write_handoff_state_file(smoke_run_handoff, state_path)

    resolved = resolve_test_job_handoff_or_none(
        {SMOKE_HANDOFF_FILE_ENV_VAR: str(state_path)}, tier=str(smoke_run_handoff.tier)
    )

    assert resolved == smoke_run_handoff


def test_the_test_job_has_no_handoff_when_the_environment_names_none(smoke_run_handoff: SmokeRunHandoff) -> None:
    assert resolve_test_job_handoff_or_none({"PATH": "/usr/bin"}, tier=str(smoke_run_handoff.tier)) is None
