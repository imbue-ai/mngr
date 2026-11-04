from pathlib import Path
from pathlib import PurePosixPath

from imbue.minds.smoke_tests.workspace_exec import app_mngr_environment
from imbue.minds.smoke_tests.workspace_exec import workspace_exec_address
from imbue.minds.smoke_tests.workspace_exec import workspace_exec_argv


def test_a_workspace_is_addressed_on_its_own_host_and_provider() -> None:
    address = workspace_exec_address(
        agent_id="agent-0123456789abcdef", host_id="host-fedcba9876543210", provider_name="imbue_cloud_alice-imbue-com"
    )

    assert address == "agent-0123456789abcdef@host-fedcba9876543210.imbue_cloud_alice-imbue-com"


def test_a_workspace_whose_host_is_not_known_is_addressed_by_its_bare_id() -> None:
    assert workspace_exec_address(agent_id="agent-1", host_id="host-1", provider_name=None) == "agent-1"
    assert workspace_exec_address(agent_id="agent-1", host_id=None, provider_name="imbue_cloud_alice") == "agent-1"


def test_the_exec_runs_the_command_from_the_given_directory_without_starting_a_stopped_host() -> None:
    argv = workspace_exec_argv(
        mngr_executable=Path("/data/production/state/.venv/bin/mngr"),
        workspace_address="agent-1@host-1.imbue_cloud_alice",
        command="echo ready",
        cwd=PurePosixPath("/home/user/workspace"),
        timeout_seconds=120.0,
    )

    assert argv == [
        "/data/production/state/.venv/bin/mngr",
        "exec",
        "agent-1@host-1.imbue_cloud_alice",
        "echo ready",
        "--cwd",
        "/home/user/workspace",
        "--no-start",
        "--timeout",
        "120.0",
        "--format",
        "json",
    ]


def test_mngr_runs_on_the_app_s_environment_with_the_app_s_host_dir_and_prefix() -> None:
    environ = {
        "HOME": "/home/runner",
        "PATH": "/usr/bin",
        "PYTEST_CURRENT_TEST": "apps/minds/smoke_tests/test_smoke_journey.py::test_it (call)",
        "MNGR_HOST_DIR": "/home/runner/.mngr-test-1234",
        "MNGR_PREFIX": "mngr-test-1234-",
    }

    environment = app_mngr_environment(environ, Path("/data/production/state/mngr"), "minds-")

    assert environment == {
        "HOME": "/home/runner",
        "PATH": "/usr/bin",
        "MNGR_HOST_DIR": "/data/production/state/mngr",
        "MNGR_PREFIX": "minds-",
    }
