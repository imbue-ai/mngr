import json
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.testing import UPGRADABLE_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import SKIP_ABORTED
from imbue.mngr.api.upgrade import SKIP_EXITED
from imbue.mngr.api.upgrade import SKIP_MID_TURN
from imbue.mngr.api.upgrade import SKIP_REPLACED
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradeOutcomeHost
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import UpgradeSkip
from imbue.mngr.api.upgrade import UpgradeSkipKind
from imbue.mngr.cli.exit_codes import EXIT_CODE_AGENTS_NOT_UPGRADED
from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.cli.upgrade import build_upgrade_result_data
from imbue.mngr.cli.upgrade import build_upgrade_summary_data
from imbue.mngr.cli.upgrade import emit_upgrade_outcomes
from imbue.mngr.cli.upgrade import format_upgrade_outcome
from imbue.mngr.cli.upgrade import format_upgrade_summary
from imbue.mngr.cli.upgrade import get_upgrade_exit_code
from imbue.mngr.cli.upgrade import get_upgrade_outcome_event_name
from imbue.mngr.cli.upgrade import upgrade
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UpgradeSkipDetail

_ID: Final[AgentId] = AgentId.generate()
_NAME: Final[AgentName] = AgentName("ov-a")
_HOST: Final[UpgradeOutcomeHost] = UpgradeOutcomeHost(
    id=HostId.generate(), name=HostName("localhost"), provider_name=LOCAL_PROVIDER_NAME
)
_DOCKER_HOST: Final[UpgradeOutcomeHost] = UpgradeOutcomeHost(
    id=HostId.generate(), name=HostName("box-1"), provider_name=ProviderInstanceName("docker")
)
_HOST_DATA: Final[dict[str, str]] = {"id": str(_HOST.id), "name": "localhost", "provider_name": "local"}
_CHANGED_PATHS: Final[tuple[Path, ...]] = (Path("commands/a.sh"), Path("settings.json"))


def _upgraded(plan: UpgradePlan, prior_state: AgentLifecycleState, is_dry_run: bool) -> AgentUpgraded:
    return AgentUpgraded(
        agent_id=_ID,
        agent_name=_NAME,
        host=_HOST,
        plan=plan,
        prior_state=prior_state,
        changed_paths=_CHANGED_PATHS,
        is_dry_run=is_dry_run,
    )


def _skipped(skip: UpgradeSkip, is_dry_run: bool) -> AgentUpgradeSkipped:
    return AgentUpgradeSkipped(
        agent_id=_ID, agent_name=_NAME, host=_HOST, kind=skip.kind, detail=skip.detail, is_dry_run=is_dry_run
    )


def _failed(step: UpgradeFailedStep, error_message: str) -> AgentUpgradeFailed:
    return AgentUpgradeFailed(agent_id=_ID, agent_name=_NAME, host=_HOST, step=step, error_message=error_message)


_UPGRADED: Final[AgentUpgraded] = _upgraded(UpgradePlan.RESTART_IDLE, AgentLifecycleState.WAITING, is_dry_run=False)
_CURRENT: Final[AgentCurrent] = AgentCurrent(agent_id=_ID, agent_name=_NAME, host=_HOST, is_dry_run=False)
_BUSY: Final[AgentUpgradeSkipped] = _skipped(SKIP_MID_TURN, is_dry_run=False)
_ABORTED: Final[AgentUpgradeSkipped] = _skipped(SKIP_ABORTED, is_dry_run=False)
_FAILED: Final[AgentUpgradeFailed] = _failed(UpgradeFailedStep.PREPARE, "no hooks")


@pytest.mark.parametrize(
    ("outcome", "expected_line"),
    [
        (_UPGRADED, "upgraded ov-a (restarted)"),
        (
            _upgraded(UpgradePlan.LEAVE_STOPPED, AgentLifecycleState.STOPPED, is_dry_run=False),
            "upgraded ov-a (stopped)",
        ),
        (
            _upgraded(UpgradePlan.RESTART_INTERRUPTING, AgentLifecycleState.RUNNING, is_dry_run=False),
            "upgraded ov-a (restarted; interrupted RUNNING)",
        ),
        (
            _upgraded(UpgradePlan.RESTART_IDLE, AgentLifecycleState.WAITING, is_dry_run=True),
            "would upgrade ov-a (restart)\n  commands/a.sh\n  settings.json",
        ),
        (
            _upgraded(UpgradePlan.RESTART_INTERRUPTING, AgentLifecycleState.WAITING, is_dry_run=True),
            "would upgrade ov-a (interrupts WAITING)\n  commands/a.sh\n  settings.json",
        ),
        (
            _upgraded(UpgradePlan.RESTART_INTERRUPTING, AgentLifecycleState.DONE, is_dry_run=True),
            "would upgrade ov-a (restart; its program has exited)\n  commands/a.sh\n  settings.json",
        ),
        (
            _upgraded(UpgradePlan.RESTART_INTERRUPTING, AgentLifecycleState.DONE, is_dry_run=False),
            "upgraded ov-a (restarted; its program had exited)",
        ),
        (
            AgentUpgraded(
                agent_id=_ID,
                agent_name=_NAME,
                host=_HOST,
                plan=UpgradePlan.LEAVE_STOPPED,
                prior_state=AgentLifecycleState.STOPPED,
                changed_paths=(Path("commands/a.sh"),),
                is_dry_run=True,
            ),
            "would upgrade ov-a (stopped)\n  commands/a.sh",
        ),
        (_CURRENT, "current ov-a"),
        (AgentCurrent(agent_id=_ID, agent_name=_NAME, host=_HOST, is_dry_run=True), "current ov-a"),
        (
            AgentCurrent(agent_id=_ID, agent_name=_NAME, host=_DOCKER_HOST, is_dry_run=False),
            "current ov-a@box-1.docker",
        ),
        (_BUSY, f"skipped ov-a: {SKIP_MID_TURN.detail}"),
        (_skipped(SKIP_MID_TURN, is_dry_run=True), f"would skip ov-a: {SKIP_MID_TURN.detail}"),
        (_skipped(SKIP_EXITED, is_dry_run=False), "skipped ov-a: its program has exited (--force restarts it)"),
        (
            _skipped(SKIP_REPLACED, is_dry_run=False),
            "skipped ov-a: another program is running in its terminal (--force ends it and restarts the agent)",
        ),
        (
            AgentUpgradeSkipped(
                agent_id=_ID,
                agent_name=_NAME,
                host=_DOCKER_HOST,
                kind=UpgradeSkipKind.NOT_IDLE,
                detail=UpgradeSkipDetail("its host is stopped (`mngr start ov-a@box-1.docker` starts it)"),
                is_dry_run=False,
            ),
            "skipped ov-a@box-1.docker: its host is stopped (`mngr start ov-a@box-1.docker` starts it)",
        ),
        (
            AgentUpgradeSkipped(
                agent_id=_ID,
                agent_name=_NAME,
                host=_DOCKER_HOST,
                kind=UpgradeSkipKind.NOT_IDLE,
                detail=UpgradeSkipDetail(
                    "its host could not be reached: rsync failed: Connection closed\nrsync error: code 255\n"
                ),
                is_dry_run=False,
            ),
            "skipped ov-a@box-1.docker: "
            "its host could not be reached: rsync failed: Connection closed; rsync error: code 255",
        ),
        (
            _skipped(
                UpgradeSkip(
                    kind=UpgradeSkipKind.UNSUPPORTED,
                    detail=UpgradeSkipDetail("agent type 'ov-type' has no upgrade support"),
                ),
                is_dry_run=False,
            ),
            "skipped ov-a: agent type 'ov-type' has no upgrade support",
        ),
        (_failed(UpgradeFailedStep.LOCK, "lock held"), "failed ov-a: lock (lock held); untouched"),
        (_FAILED, "failed ov-a: prepare (no hooks); untouched"),
        (_failed(UpgradeFailedStep.START_CHECK, "no work dir"), "failed ov-a: start_check (no work dir); untouched"),
        (
            _failed(UpgradeFailedStep.STOP, "tmux hung"),
            "failed ov-a: stop (tmux hung); files unchanged; the agent may be stopped",
        ),
        (
            _failed(UpgradeFailedStep.SWAP, "disk full"),
            "failed ov-a: swap (disk full); left stopped; some files may already be replaced",
        ),
        (
            _failed(UpgradeFailedStep.SWAP, "mv: cannot move\n\n  disk full\n"),
            "failed ov-a: swap (mv: cannot move; disk full); left stopped; some files may already be replaced",
        ),
        (
            _failed(UpgradeFailedStep.START, "no work dir"),
            "failed ov-a: start (no work dir); "
            "not running on the new files; start it with `mngr start ov-a --restart`",
        ),
        (
            _failed(UpgradeFailedStep.READY, "dialog"),
            "failed ov-a: ready (dialog); started but did not signal readiness; session left for inspection; "
            "restart it with `mngr start ov-a --restart`",
        ),
        (
            _failed(UpgradeFailedStep.UNLOCK, "Host lock was lost"),
            "failed ov-a: unlock (Host lock was lost); "
            "its upgrade ran, but another mngr may have changed it while the host lock was not held",
        ),
        (
            AgentUpgradeFailed(
                agent_id=_ID, agent_name=_NAME, host=_DOCKER_HOST, step=UpgradeFailedStep.START, error_message="gone"
            ),
            "failed ov-a@box-1.docker: start (gone); "
            "not running on the new files; start it with `mngr start ov-a@box-1.docker --restart`",
        ),
    ],
)
def test_format_upgrade_outcome_names_the_agent_and_what_happened_to_it(
    outcome: AgentUpgradeOutcome, expected_line: str
) -> None:
    assert format_upgrade_outcome(outcome) == expected_line


@pytest.mark.parametrize(
    ("outcomes", "expected_exit_code"),
    [
        ([], EXIT_CODE_SUCCESS),
        ([_UPGRADED, _CURRENT], EXIT_CODE_SUCCESS),
        ([_upgraded(UpgradePlan.RESTART_IDLE, AgentLifecycleState.WAITING, is_dry_run=True)], EXIT_CODE_SUCCESS),
        ([_UPGRADED, _BUSY, _CURRENT], EXIT_CODE_AGENTS_NOT_UPGRADED),
        ([_ABORTED], EXIT_CODE_AGENTS_NOT_UPGRADED),
        ([_skipped(SKIP_MID_TURN, is_dry_run=True)], EXIT_CODE_AGENTS_NOT_UPGRADED),
        ([_FAILED], EXIT_CODE_ERROR),
        ([_BUSY, _UPGRADED, _FAILED, _ABORTED], EXIT_CODE_ERROR),
    ],
)
def test_get_upgrade_exit_code_puts_a_failure_above_an_agent_left_out_of_date(
    outcomes: Sequence[AgentUpgradeOutcome], expected_exit_code: int
) -> None:
    assert get_upgrade_exit_code(outcomes) == expected_exit_code


@pytest.mark.parametrize(
    ("outcome", "expected_event_name"),
    [
        (_UPGRADED, "agent_upgraded"),
        (_CURRENT, "agent_current"),
        (_BUSY, "agent_skipped"),
        (_FAILED, "agent_failed"),
    ],
)
def test_get_upgrade_outcome_event_name_names_the_kind_of_outcome(
    outcome: AgentUpgradeOutcome, expected_event_name: str
) -> None:
    assert get_upgrade_outcome_event_name(outcome) == expected_event_name


def test_format_upgrade_summary_counts_each_kind_of_outcome() -> None:
    outcomes = [_UPGRADED, _UPGRADED, _CURRENT, _BUSY, _FAILED, _ABORTED]

    assert format_upgrade_summary(outcomes, is_dry_run=False) == "2 upgraded, 1 current, 2 not upgraded, 1 failed"
    assert format_upgrade_summary(outcomes, is_dry_run=True) == (
        "2 would be upgraded, 1 current, 2 would not be upgraded, 1 failed"
    )


def test_build_upgrade_summary_data_carries_the_counts_and_the_exit_code() -> None:
    assert build_upgrade_summary_data([_UPGRADED, _CURRENT, _BUSY], is_dry_run=True) == {
        "dry_run": True,
        "upgraded_count": 1,
        "current_count": 1,
        "skipped_count": 1,
        "failed_count": 0,
        "exit_code": EXIT_CODE_AGENTS_NOT_UPGRADED,
    }


def test_build_upgrade_result_data_lists_every_outcomes_fields_by_kind() -> None:
    result_data = build_upgrade_result_data([_UPGRADED, _CURRENT, _BUSY, _FAILED, _ABORTED], is_dry_run=False)

    assert result_data == {
        "upgraded_agents": [
            {
                "agent_id": str(_ID),
                "agent_name": "ov-a",
                "host": _HOST_DATA,
                "plan": "RESTART_IDLE",
                "prior_state": "WAITING",
                "changed_paths": ["commands/a.sh", "settings.json"],
            }
        ],
        "current_agents": [{"agent_id": str(_ID), "agent_name": "ov-a", "host": _HOST_DATA}],
        "skipped_agents": [
            {
                "agent_id": str(_ID),
                "agent_name": "ov-a",
                "host": _HOST_DATA,
                "kind": "NOT_IDLE",
                "detail": SKIP_MID_TURN.detail,
            },
            {
                "agent_id": str(_ID),
                "agent_name": "ov-a",
                "host": _HOST_DATA,
                "kind": "ABORTED",
                "detail": SKIP_ABORTED.detail,
            },
        ],
        "failed_agents": [
            {
                "agent_id": str(_ID),
                "agent_name": "ov-a",
                "host": _HOST_DATA,
                "step": "PREPARE",
                "error_message": "no hooks",
            }
        ],
        "dry_run": False,
        "upgraded_count": 1,
        "current_count": 1,
        "skipped_count": 2,
        "failed_count": 1,
        "exit_code": EXIT_CODE_ERROR,
    }


def _yield_recording_the_output_before_each(
    outcomes: Sequence[AgentUpgradeOutcome],
    capsys: pytest.CaptureFixture[str],
    output_before_each: list[str],
) -> Iterator[AgentUpgradeOutcome]:
    for outcome in outcomes:
        output_before_each.append(capsys.readouterr().out)
        yield outcome


@pytest.mark.parametrize(
    ("output_format", "expected_first_output"),
    [
        pytest.param(OutputFormat.HUMAN, "upgraded ov-a (restarted)\n", id="human"),
        pytest.param(
            OutputFormat.JSONL,
            json.dumps(
                {
                    "event": "agent_upgraded",
                    "agent_id": str(_ID),
                    "agent_name": "ov-a",
                    "host": _HOST_DATA,
                    "plan": "RESTART_IDLE",
                    "prior_state": "WAITING",
                    "changed_paths": ["commands/a.sh", "settings.json"],
                    "dry_run": False,
                }
            )
            + "\n",
            id="jsonl",
        ),
        pytest.param(OutputFormat.JSON, "", id="json"),
    ],
)
def test_emit_upgrade_outcomes_writes_an_agents_outcome_before_the_next_agent_is_upgraded(
    capsys: pytest.CaptureFixture[str], output_format: OutputFormat, expected_first_output: str
) -> None:
    output_before_each: list[str] = []

    emitted_outcomes = emit_upgrade_outcomes(
        _yield_recording_the_output_before_each([_UPGRADED, _CURRENT], capsys, output_before_each),
        is_dry_run=False,
        output_format=output_format,
    )

    assert output_before_each == ["", expected_first_output]
    assert emitted_outcomes == [_UPGRADED, _CURRENT]


def test_upgrade_help_shows_the_default_of_on_error(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(upgrade, ["--help"])

    assert result.exit_code == 0
    assert "[default: continue]" in result.output


def test_upgrade_requires_an_agent(cli_runner: CliRunner, plugin_manager: pluggy.PluginManager) -> None:
    result = cli_runner.invoke(upgrade, [], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code == 2
    assert "Must specify at least one agent" in result.output


def test_upgrade_of_an_unknown_agent_fails_before_touching_any_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
    temp_work_dir: Path,
) -> None:
    agent = create_test_agent_state(local_host, temp_work_dir, "unknown-peer-48213", UPGRADABLE_COMMAND_AGENT_TYPE)

    result = cli_runner.invoke(
        upgrade, ["unknown-peer-48213", "no-such-agent-48213"], obj=plugin_manager, catch_exceptions=True
    )

    assert result.exit_code != 0
    assert "No agent(s) found matching: no-such-agent-48213" in result.output
    assert not (get_agent_state_dir_path(local_host.host_dir, agent.id) / "commands" / UPGRADED_SCRIPT_NAME).exists()
