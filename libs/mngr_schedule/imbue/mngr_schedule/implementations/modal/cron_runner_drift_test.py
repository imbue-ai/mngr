"""Drift tests for the constants inlined in cron_runner.py and verification.py.

cron_runner.py is deployed standalone into Modal, where the Python
interpreter does NOT see the imbue namespace. It cannot import from
`imbue.mngr.primitives` or `imbue.mngr_schedule.data_types` at module
scope, so values that mirror those enums (RUNNING_STATES,
VALID_VERIFY_MODES) and sentinel values shared with verification.py
(AGENT_MISSING_STATE, RESULT_SENTINEL) are duplicated as bare literals.

These tests import cron_runner.py directly (with stubbed deploy-time
env vars) and compare the literal values against the authoritative
imbue enums. Direct import is the only place this module gets pulled
in by the rest of the test suite -- everything else either lives in
the cron container or in deploy.py / verification.py (which import
neither cron_runner nor the cron-only constants module).

Why not AST-parse the source? AST parsing was the original approach
but is brittle to formatting changes (e.g. switching `frozenset({...})`
to a comprehension would break the parser). Importing under stub env
catches refactors a parser would miss.
"""

import json
import os
import sys
from collections.abc import Iterator
from types import ModuleType

import pytest

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr_schedule.data_types import VerifyMode
from imbue.mngr_schedule.implementations.modal import verification

_CRON_RUNNER_MODULE: str = "imbue.mngr_schedule.implementations.modal.cron_runner"


@pytest.fixture(scope="module")
def cron_runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    """Import cron_runner.py under stubbed deploy-time env vars.

    cron_runner.py reads required env vars at module scope under
    `modal.is_local()` and would raise on a developer machine without
    them. Set placeholder values, write a stub Dockerfile so the
    `modal.Image.from_dockerfile` call has a real path, then import.
    The image itself is lazy at import (Modal only builds it during
    `modal deploy`), so a placeholder Dockerfile and empty context dir
    are sufficient.

    Restores prior env / sys.modules state on teardown so other tests
    in the session aren't affected.
    """
    tmp = tmp_path_factory.mktemp("cron_runner_drift_stub")
    dockerfile = tmp / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\n")

    deploy_config = {
        "app_name": "cron-runner-drift-test",
        "cron_schedule": "0 0 * * *",
        "cron_timezone": "UTC",
    }
    env_overrides = {
        "SCHEDULE_DEPLOY_CONFIG": json.dumps(deploy_config),
        "SCHEDULE_BUILD_CONTEXT_DIR": str(tmp),
        "SCHEDULE_STAGING_DIR": str(tmp),
        "SCHEDULE_DOCKERFILE": str(dockerfile),
    }
    saved_env = {k: os.environ.get(k) for k in env_overrides}
    saved_module = sys.modules.pop(_CRON_RUNNER_MODULE, None)
    try:
        os.environ.update(env_overrides)
        import imbue.mngr_schedule.implementations.modal.cron_runner as cron_runner_module

        yield cron_runner_module
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop(_CRON_RUNNER_MODULE, None)
        if saved_module is not None:
            sys.modules[_CRON_RUNNER_MODULE] = saved_module


def test_cron_runner_running_states_match_lifecycle_enum(cron_runner: ModuleType) -> None:
    expected = frozenset(
        {
            AgentLifecycleState.RUNNING.value,
            AgentLifecycleState.WAITING.value,
            AgentLifecycleState.REPLACED.value,
            AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE.value,
        }
    )
    assert cron_runner.RUNNING_STATES == expected


def test_agent_lifecycle_state_enum_pinned() -> None:
    """Pin the full enum so any addition or removal forces a manual reconcile.

    Adding a new RUNNING_* variant upstream without updating cron_runner's
    inlined RUNNING_STATES would silently treat the new state as terminal in
    full-verify polling. Pin the closed set here to surface the change.
    """
    assert {state.value for state in AgentLifecycleState} == {
        "RUNNING",
        "WAITING",
        "REPLACED",
        "RUNNING_UNKNOWN_AGENT_TYPE",
        "STOPPED",
        "DONE",
    }


def test_cron_runner_valid_verify_modes_match_verify_mode_enum(cron_runner: ModuleType) -> None:
    assert cron_runner.VALID_VERIFY_MODES == frozenset(mode.value.lower() for mode in VerifyMode)


def test_agent_missing_state_matches_between_files(cron_runner: ModuleType) -> None:
    assert cron_runner.AGENT_MISSING_STATE == verification._AGENT_MISSING_STATE


def test_agent_missing_state_disjoint_from_lifecycle_enum(cron_runner: ModuleType) -> None:
    """The sentinel must not collide with any real lifecycle state."""
    assert cron_runner.AGENT_MISSING_STATE not in {state.value for state in AgentLifecycleState}


def test_result_sentinel_matches_between_files(cron_runner: ModuleType) -> None:
    assert cron_runner.RESULT_SENTINEL == verification._RESULT_SENTINEL
