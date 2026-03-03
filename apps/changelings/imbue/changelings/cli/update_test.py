import json

import pytest
from click.testing import CliRunner

from imbue.changelings.cli.update import _is_agent_remote
from imbue.changelings.cli.update import _print_result
from imbue.changelings.deployment.local import UpdateResult
from imbue.changelings.main import cli
from imbue.changelings.primitives import AgentName
from imbue.changelings.testing import FakeConcurrencyGroup
from imbue.changelings.testing import capture_loguru_messages
from imbue.changelings.testing import make_fake_concurrency_group
from imbue.changelings.testing import make_finished_process

_RUNNER = CliRunner()


def _make_list_cg(provider: str) -> FakeConcurrencyGroup:
    """Create a FakeConcurrencyGroup that returns mng list JSON for the given provider."""
    return make_fake_concurrency_group(
        results={
            "list": make_finished_process(
                stdout=json.dumps(
                    {
                        "agents": [
                            {
                                "id": "agent-abc123",
                                "name": "my-agent",
                                "host": {"provider_name": provider, "state": "RUNNING"},
                            }
                        ]
                    }
                ),
                command=("mng", "list"),
            ),
        }
    )


def test_update_requires_agent_name() -> None:
    result = _RUNNER.invoke(cli, ["update"])

    assert result.exit_code != 0
    assert "Missing argument" in result.output


def test_update_help_shows_flags() -> None:
    result = _RUNNER.invoke(cli, ["update", "--help"])

    assert result.exit_code == 0
    assert "--snapshot" in result.output
    assert "--no-snapshot" in result.output
    assert "--push" in result.output
    assert "--no-push" in result.output
    assert "--provision" in result.output
    assert "--no-provision" in result.output


def test_update_help_describes_steps() -> None:
    result = _RUNNER.invoke(cli, ["update", "--help"])

    assert result.exit_code == 0
    assert "snapshot" in result.output.lower()
    assert "AGENT_NAME" in result.output


def test_update_shows_in_cli_help() -> None:
    result = _RUNNER.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "update" in result.output


# --- _is_agent_remote tests ---


def test_is_agent_remote_returns_false_for_local() -> None:
    """Verify _is_agent_remote returns False for a local agent."""
    cg = _make_list_cg("local")

    assert _is_agent_remote(AgentName("my-agent"), concurrency_group=cg) is False


def test_is_agent_remote_returns_true_for_modal() -> None:
    """Verify _is_agent_remote returns True for a modal agent."""
    cg = _make_list_cg("modal")

    assert _is_agent_remote(AgentName("my-agent"), concurrency_group=cg) is True


def test_is_agent_remote_returns_true_for_docker() -> None:
    """Verify _is_agent_remote returns True for a docker agent."""
    cg = _make_list_cg("docker")

    assert _is_agent_remote(AgentName("my-agent"), concurrency_group=cg) is True


def test_is_agent_remote_returns_false_on_failure() -> None:
    """Verify _is_agent_remote returns False when the check fails (fail-open)."""
    cg = make_fake_concurrency_group(
        results={
            "list": make_finished_process(returncode=1, stderr="error", command=("mng", "list")),
        }
    )

    assert _is_agent_remote(AgentName("my-agent"), concurrency_group=cg) is False


def test_is_agent_remote_returns_false_on_invalid_json() -> None:
    """Verify _is_agent_remote returns False when JSON parsing fails."""
    cg = make_fake_concurrency_group(
        results={
            "list": make_finished_process(stdout="not valid json {{{", command=("mng", "list")),
        }
    )

    assert _is_agent_remote(AgentName("my-agent"), concurrency_group=cg) is False


def test_is_agent_remote_returns_false_when_agent_not_found() -> None:
    """Verify _is_agent_remote returns False when no agents match."""
    cg = make_fake_concurrency_group(
        results={
            "list": make_finished_process(
                stdout=json.dumps({"agents": []}),
                command=("mng", "list"),
            ),
        }
    )

    assert _is_agent_remote(AgentName("ghost"), concurrency_group=cg) is False


# --- _print_result tests ---


@pytest.mark.parametrize(
    "did_snapshot, did_push, did_provision",
    [
        (True, True, True),
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_print_result_includes_agent_name_and_steps(
    did_snapshot: bool,
    did_push: bool,
    did_provision: bool,
) -> None:
    """Verify _print_result outputs agent name and completed steps."""
    result = UpdateResult(
        agent_name=AgentName("my-agent"),
        did_snapshot=did_snapshot,
        did_push=did_push,
        did_provision=did_provision,
    )

    with capture_loguru_messages() as messages:
        _print_result(result)

    combined = "".join(messages)
    assert "my-agent" in combined
    assert "updated successfully" in combined.lower()
    if did_snapshot:
        assert "snapshot" in combined.lower()
    if did_push:
        assert "push" in combined.lower()
    if did_provision:
        assert "provision" in combined.lower()
