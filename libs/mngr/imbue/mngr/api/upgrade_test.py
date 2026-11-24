from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.default_plugins.headless_command_agent import HeadlessCommand
from imbue.mngr.agents.default_plugins.headless_command_agent import HeadlessCommandConfig
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.testing import LockRecordingHost
from imbue.mngr.api.testing import ScriptedStateCommandAgent
from imbue.mngr.api.testing import UpgradableCommandAgent
from imbue.mngr.api.testing import make_recorded_upgradable_agent
from imbue.mngr.api.testing import make_skipped_outcome
from imbue.mngr.api.testing import make_upgradable_command_agent
from imbue.mngr.api.testing import make_upgrade_outcome_host
from imbue.mngr.api.testing import upgrade_agent
from imbue.mngr.api.testing import write_command_agent_record
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import MatchedHost
from imbue.mngr.api.upgrade import NotOnlineHost
from imbue.mngr.api.upgrade import ReachedHost
from imbue.mngr.api.upgrade import SKIP_ABORTED
from imbue.mngr.api.upgrade import SKIP_BACKGROUND_WORK
from imbue.mngr.api.upgrade import SKIP_DESTROYED
from imbue.mngr.api.upgrade import SKIP_EXITED
from imbue.mngr.api.upgrade import SKIP_IS_CALLER
from imbue.mngr.api.upgrade import SKIP_MID_TURN
from imbue.mngr.api.upgrade import SKIP_ON_DIALOG
from imbue.mngr.api.upgrade import SKIP_REPLACED
from imbue.mngr.api.upgrade import SKIP_STATE_UNREADABLE
from imbue.mngr.api.upgrade import UnreachableHost
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradeOutcomeHost
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import UpgradeSkip
from imbue.mngr.api.upgrade import UpgradeSkipKind
from imbue.mngr.api.upgrade import _append_recreate_command
from imbue.mngr.api.upgrade import format_agent_label
from imbue.mngr.api.upgrade import is_upgrade_caller
from imbue.mngr.api.upgrade import iter_upgrade_outcomes
from imbue.mngr.api.upgrade import parse_upgrade_inputs_output
from imbue.mngr.api.upgrade import plan_agent_upgrade
from imbue.mngr.api.upgrade import upgrade_matched_agent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import InvalidUpgradeInputError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UpgradeSkipDetail
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_placeholder_agent_type
from imbue.mngr.utils.testing import make_command_agent
from imbue.mngr.utils.testing import make_local_host_of_class

_ESCAPING_RELATIVE_PATH: Final[Path] = Path("..") / "escaped-48213"

_ESCAPING_ABSOLUTE_PATH: Final[Path] = Path("/escaped-48213")


def _match(agent_name: str, provider_name: ProviderInstanceName) -> AgentMatch:
    return AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName(agent_name),
        host_id=HostId.generate(),
        host_name=HostName("host"),
        provider_name=provider_name,
    )


def _host_of_match(match: AgentMatch) -> UpgradeOutcomeHost:
    return UpgradeOutcomeHost(id=match.host_id, name=match.host_name, provider_name=match.provider_name)


def _skipped_match(match: AgentMatch, skip: UpgradeSkip, is_dry_run: bool) -> AgentUpgradeSkipped:
    return AgentUpgradeSkipped(
        agent_id=match.agent_id,
        agent_name=match.agent_name,
        host=_host_of_match(match),
        kind=skip.kind,
        detail=skip.detail,
        is_dry_run=is_dry_run,
    )


@pytest.mark.parametrize(
    ("state", "is_blocked_on_dialog", "is_forced", "expected"),
    [
        (AgentLifecycleState.STOPPED, False, False, UpgradePlan.LEAVE_STOPPED),
        (AgentLifecycleState.STOPPED, False, True, UpgradePlan.LEAVE_STOPPED),
        (AgentLifecycleState.WAITING, False, False, UpgradePlan.RESTART_IDLE),
        (AgentLifecycleState.WAITING, False, True, UpgradePlan.RESTART_IDLE),
        (AgentLifecycleState.WAITING, True, False, SKIP_ON_DIALOG),
        (AgentLifecycleState.WAITING, True, True, UpgradePlan.RESTART_INTERRUPTING),
        (AgentLifecycleState.RUNNING, False, False, SKIP_MID_TURN),
        (AgentLifecycleState.RUNNING, False, True, UpgradePlan.RESTART_INTERRUPTING),
        (AgentLifecycleState.RUNNING_BACKGROUND, False, False, SKIP_BACKGROUND_WORK),
        (AgentLifecycleState.RUNNING_BACKGROUND, False, True, UpgradePlan.RESTART_INTERRUPTING),
        (AgentLifecycleState.DONE, False, False, SKIP_EXITED),
        (AgentLifecycleState.DONE, False, True, UpgradePlan.RESTART_INTERRUPTING),
        (AgentLifecycleState.REPLACED, False, False, SKIP_REPLACED),
        (AgentLifecycleState.REPLACED, False, True, UpgradePlan.RESTART_INTERRUPTING),
        (AgentLifecycleState.UNKNOWN, False, False, SKIP_STATE_UNREADABLE),
        (AgentLifecycleState.UNKNOWN, False, True, SKIP_STATE_UNREADABLE),
        (AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE, False, False, SKIP_STATE_UNREADABLE),
        (AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE, False, True, SKIP_STATE_UNREADABLE),
    ],
)
def test_plan_agent_upgrade_restarts_only_an_idle_agent_unless_forced(
    state: AgentLifecycleState,
    is_blocked_on_dialog: bool,
    is_forced: bool,
    expected: UpgradePlan | UpgradeSkip,
) -> None:
    assert plan_agent_upgrade(state, is_blocked_on_dialog, is_forced) == expected


@pytest.mark.parametrize(
    ("skip", "expected_kind"),
    [
        pytest.param(SKIP_MID_TURN, UpgradeSkipKind.NOT_IDLE, id="mid_turn"),
        pytest.param(SKIP_ON_DIALOG, UpgradeSkipKind.NOT_IDLE, id="on_a_dialog"),
        pytest.param(SKIP_BACKGROUND_WORK, UpgradeSkipKind.NOT_IDLE, id="running_background_work"),
        pytest.param(SKIP_REPLACED, UpgradeSkipKind.NOT_IDLE, id="another_program_in_its_terminal"),
        pytest.param(SKIP_EXITED, UpgradeSkipKind.NOT_IDLE, id="program_exited"),
        pytest.param(SKIP_STATE_UNREADABLE, UpgradeSkipKind.NOT_IDLE, id="state_unreadable"),
        pytest.param(SKIP_IS_CALLER, UpgradeSkipKind.NOT_IDLE, id="running_the_command"),
        pytest.param(SKIP_DESTROYED, UpgradeSkipKind.NOT_IDLE, id="destroyed_during_the_run"),
        pytest.param(
            _append_recreate_command("its launch command loads no file of mngr's", "ov-a"),
            UpgradeSkipKind.UNSUPPORTED,
            id="the_harnesss_detail",
        ),
        pytest.param(SKIP_ABORTED, UpgradeSkipKind.ABORTED, id="an_earlier_agent_failed"),
    ],
)
def test_each_skip_carries_the_kind_that_names_what_it_is_about(
    skip: UpgradeSkip, expected_kind: UpgradeSkipKind
) -> None:
    assert skip.kind == expected_kind


def _fail_the_agent_named_second(match: AgentMatch) -> AgentUpgradeOutcome:
    """A swap failure for the agent named ``second``, and a real run's upgrade of a stopped agent for any other."""
    if match.agent_name == "second":
        return AgentUpgradeFailed(
            agent_id=match.agent_id,
            agent_name=match.agent_name,
            host=_host_of_match(match),
            step=UpgradeFailedStep.SWAP,
            error_message="disk full",
        )
    return AgentUpgraded(
        agent_id=match.agent_id,
        agent_name=match.agent_name,
        host=_host_of_match(match),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=(Path("commands/changed.sh"),),
        is_dry_run=False,
    )


@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_iter_upgrade_outcomes_under_abort_skips_every_agent_after_the_first_failure(is_dry_run: bool) -> None:
    matches = [_match(name, LOCAL_PROVIDER_NAME) for name in ("first", "second", "third", "fourth")]
    attempted_names: list[AgentName] = []

    def upgrade_one_agent(match: AgentMatch) -> AgentUpgradeOutcome:
        attempted_names.append(match.agent_name)
        return _fail_the_agent_named_second(match)

    outcomes = list(iter_upgrade_outcomes(matches, upgrade_one_agent, ErrorBehavior.ABORT, is_dry_run=is_dry_run))

    assert attempted_names == ["first", "second"]
    assert outcomes == [
        *(_fail_the_agent_named_second(match) for match in matches[:2]),
        *(_skipped_match(match, SKIP_ABORTED, is_dry_run) for match in matches[2:]),
    ]


@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_iter_upgrade_outcomes_under_continue_attempts_every_agent_after_a_failure(is_dry_run: bool) -> None:
    matches = [_match(name, LOCAL_PROVIDER_NAME) for name in ("first", "second", "third")]

    outcomes = list(
        iter_upgrade_outcomes(matches, _fail_the_agent_named_second, ErrorBehavior.CONTINUE, is_dry_run=is_dry_run)
    )

    assert outcomes == [_fail_the_agent_named_second(match) for match in matches]
    assert isinstance(outcomes[1], AgentUpgradeFailed)


@pytest.mark.parametrize("error_behavior", list(ErrorBehavior))
def test_iter_upgrade_outcomes_attempts_every_agent_when_none_fails(error_behavior: ErrorBehavior) -> None:
    matches = [_match(name, LOCAL_PROVIDER_NAME) for name in ("first", "second")]

    outcomes = list(
        iter_upgrade_outcomes(
            matches,
            lambda match: AgentCurrent(
                agent_id=match.agent_id, agent_name=match.agent_name, host=_host_of_match(match), is_dry_run=False
            ),
            error_behavior,
            is_dry_run=False,
        )
    )

    assert outcomes == [
        AgentCurrent(
            agent_id=match.agent_id, agent_name=match.agent_name, host=_host_of_match(match), is_dry_run=False
        )
        for match in matches
    ]


@pytest.mark.parametrize(
    ("matched_host", "expected_detail"),
    [
        pytest.param(
            NotOnlineHost(state=HostState.STOPPED),
            "its host is stopped (`mngr start on-docker@host.docker` starts it)",
            id="stopped",
        ),
        pytest.param(
            NotOnlineHost(state=HostState.PAUSED),
            "its host is paused (`mngr start on-docker@host.docker` starts it)",
            id="paused",
        ),
        pytest.param(
            NotOnlineHost(state=HostState.CRASHED),
            "its host is crashed (`mngr start on-docker@host.docker` starts it)",
            id="crashed",
        ),
        pytest.param(NotOnlineHost(state=HostState.DESTROYED), "its host is destroyed", id="destroyed"),
        pytest.param(NotOnlineHost(state=HostState.FAILED), "its host is failed", id="failed"),
        pytest.param(
            NotOnlineHost(state=HostState.UNAUTHENTICATED), "its host is unauthenticated", id="unauthenticated"
        ),
        pytest.param(
            UnreachableHost(error_message="Connection refused"),
            "its host could not be reached: Connection refused",
            id="unreachable",
        ),
    ],
)
def test_upgrade_matched_agent_skips_an_agent_whose_host_is_offline_as_not_idle_and_says_why(
    temp_mngr_ctx: MngrContext, matched_host: MatchedHost, expected_detail: str
) -> None:
    match = _match("on-docker", ProviderInstanceName("docker"))

    outcome = upgrade_matched_agent(
        match, matched_host, is_forced=True, is_dry_run=True, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == _skipped_match(
        match,
        UpgradeSkip(kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail(expected_detail)),
        is_dry_run=True,
    )


@pytest.mark.rsync
@pytest.mark.parametrize("is_forced", [False, True], ids=["not_forced", "forced"])
def test_upgrade_matched_agent_skips_the_agent_running_the_command(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext, is_forced: bool
) -> None:
    agent = make_recorded_upgradable_agent(
        local_host, tmp_path, temp_mngr_ctx, {}, "echo new-48213\n", UpgradableCommandAgent
    )
    match = AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=local_host.id,
        host_name=HostName("host"),
        provider_name=LOCAL_PROVIDER_NAME,
    )
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)

    outcome = upgrade_matched_agent(
        match,
        ReachedHost(host=local_host, agent_by_id={agent.id: agent}),
        is_forced=is_forced,
        is_dry_run=False,
        is_caller=True,
        mngr_ctx=temp_mngr_ctx,
    )

    assert outcome == make_skipped_outcome(agent, SKIP_IS_CALLER)
    assert sorted(path.name for path in state_dir.iterdir()) == ["data.json"]


def test_upgrade_matched_agent_skips_an_agent_no_longer_on_the_host(
    local_host: Host, temp_mngr_ctx: MngrContext
) -> None:
    match = _match("destroyed", LOCAL_PROVIDER_NAME)

    outcome = upgrade_matched_agent(
        match,
        ReachedHost(host=local_host, agent_by_id={}),
        is_forced=False,
        is_dry_run=False,
        is_caller=False,
        mngr_ctx=temp_mngr_ctx,
    )

    assert outcome == _skipped_match(match, SKIP_DESTROYED, is_dry_run=False)


_CALLER_AGENT_ID: Final[AgentId] = AgentId.generate()
_CALLER_HOST_ID: Final[HostId] = HostId.generate()


@pytest.mark.parametrize(
    ("agent_id", "host_id", "provider_name", "expected"),
    [
        pytest.param(_CALLER_AGENT_ID, _CALLER_HOST_ID, LOCAL_PROVIDER_NAME, True, id="the_caller"),
        pytest.param(AgentId.generate(), _CALLER_HOST_ID, LOCAL_PROVIDER_NAME, False, id="another_local_agent"),
        pytest.param(_CALLER_AGENT_ID, HostId.generate(), LOCAL_PROVIDER_NAME, False, id="same_id_on_another_host"),
        pytest.param(
            _CALLER_AGENT_ID, _CALLER_HOST_ID, ProviderInstanceName("docker"), False, id="same_ids_on_another_provider"
        ),
    ],
)
def test_is_upgrade_caller_requires_the_callers_id_on_the_local_host(
    agent_id: AgentId, host_id: HostId, provider_name: ProviderInstanceName, expected: bool
) -> None:
    match = AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("caller"),
        host_id=host_id,
        host_name=HostName("host"),
        provider_name=provider_name,
    )

    assert is_upgrade_caller(match, AgentInstanceKey.build(_CALLER_AGENT_ID, _CALLER_HOST_ID)) is expected
    assert is_upgrade_caller(match, None) is False


@pytest.mark.parametrize(
    ("provider_name", "expected_label"),
    [
        pytest.param(LOCAL_PROVIDER_NAME, "my-agent", id="local"),
        pytest.param(ProviderInstanceName("docker"), "my-agent@my-host.docker", id="remote"),
    ],
)
def test_format_agent_label_gives_an_agent_on_another_host_its_address(
    provider_name: ProviderInstanceName, expected_label: str
) -> None:
    host = UpgradeOutcomeHost(id=HostId.generate(), name=HostName("my-host"), provider_name=provider_name)

    assert format_agent_label(AgentName("my-agent"), host) == expected_label


@pytest.mark.parametrize("is_headless", [False, True], ids=["command", "headless_command"])
def test_upgrade_agent_finds_a_command_agent_current(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext, is_headless: bool
) -> None:
    command_agent, host = make_command_agent(local_provider, tmp_path / "work")
    agent = (
        HeadlessCommand.model_construct(
            id=command_agent.id,
            name=command_agent.name,
            agent_type=AgentTypeName("headless_command"),
            work_dir=command_agent.work_dir,
            create_time=command_agent.create_time,
            host_id=host.id,
            mngr_ctx=temp_mngr_ctx,
            agent_config=HeadlessCommandConfig(),
            host=host,
        )
        if is_headless
        else command_agent
    )
    write_command_agent_record(host, agent.id, agent.name, agent.work_dir, {})

    outcome = upgrade_agent(agent, host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == AgentCurrent(
        agent_id=agent.id, agent_name=agent.name, host=make_upgrade_outcome_host(host), is_dry_run=False
    )


@pytest.mark.parametrize(
    ("agent_type", "is_agent_type_registered", "expected_detail"),
    [
        pytest.param(
            "no-upgrade-support-48213",
            True,
            "agent type 'no-upgrade-support-48213' has no upgrade support",
            id="type_without_support",
        ),
        pytest.param(
            "undefined-type-48213",
            False,
            "agent type 'undefined-type-48213' is not defined in this config",
            id="type_not_defined",
        ),
    ],
)
def test_upgrade_agent_skips_an_agent_whose_class_has_no_upgrade_support_and_says_why(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    agent_type: str,
    is_agent_type_registered: bool,
    expected_detail: str,
) -> None:
    if is_agent_type_registered:
        register_placeholder_agent_type(agent_type)
    command_agent, host = make_command_agent(local_provider, tmp_path / "work")
    agent = SendKeysAgent.model_construct(
        id=command_agent.id,
        name=command_agent.name,
        agent_type=AgentTypeName(agent_type),
        work_dir=command_agent.work_dir,
        create_time=command_agent.create_time,
        host_id=host.id,
        mngr_ctx=temp_mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )

    outcome = upgrade_agent(agent, host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(
        agent, UpgradeSkip(kind=UpgradeSkipKind.UNSUPPORTED, detail=UpgradeSkipDetail(expected_detail))
    )


def test_upgrade_agent_skips_an_agent_whose_harness_says_it_cannot_load_the_upgraded_files_and_says_how_to_recreate_it(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent_id = AgentId.generate()
    write_command_agent_record(local_host, agent_id, AgentName("unsupported"), tmp_path, {})
    agent = make_upgradable_command_agent(
        local_host,
        agent_id,
        AgentName("unsupported"),
        tmp_path,
        temp_mngr_ctx,
        upgrade_unsupported_detail="its launch command loads no file of mngr's",
        upgraded_script_content="new\n",
        build_error_message=None,
        agent_class=UpgradableCommandAgent,
    )

    outcome = upgrade_agent(
        agent, local_host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == make_skipped_outcome(
        agent,
        UpgradeSkip(
            kind=UpgradeSkipKind.UNSUPPORTED,
            detail=UpgradeSkipDetail(
                "its launch command loads no file of mngr's; "
                "re-create it with `mngr create unsupported --reuse --update`"
            ),
        ),
    )


def _with_input_path(agent: UpgradableCommandAgent, path: Path) -> UpgradableCommandAgent:
    return agent.model_copy_update(to_update(agent.field_ref().upgrade_input_paths, (path,)))


def _with_upgraded_file_path(agent: UpgradableCommandAgent, path: Path) -> UpgradableCommandAgent:
    return agent.model_copy_update(to_update(agent.field_ref().upgraded_script_path, path))


def _with_ready_signal_path(agent: UpgradableCommandAgent, path: Path) -> UpgradableCommandAgent:
    return agent.model_copy_update(to_update(agent.field_ref().ready_signal_paths, (path,)))


@pytest.mark.parametrize(
    ("with_path", "escaping_path"),
    [
        pytest.param(_with_input_path, _ESCAPING_RELATIVE_PATH, id="input"),
        pytest.param(_with_upgraded_file_path, _ESCAPING_RELATIVE_PATH, id="upgraded_file"),
        pytest.param(_with_ready_signal_path, _ESCAPING_RELATIVE_PATH, id="ready_marker"),
        pytest.param(_with_ready_signal_path, _ESCAPING_ABSOLUTE_PATH, id="absolute_ready_marker"),
    ],
)
def test_upgrade_refuses_a_harness_path_outside_the_state_dir_before_stopping_an_idle_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    with_path: Callable[[UpgradableCommandAgent, Path], UpgradableCommandAgent],
    escaping_path: Path,
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    idle_agent = make_recorded_upgradable_agent(
        host, tmp_path, temp_mngr_ctx, {"command": "sleep 48213"}, "echo new-48213\n", ScriptedStateCommandAgent
    )
    agent = with_path(
        idle_agent.model_copy_update(
            to_update(idle_agent.field_ref().lifecycle_states, [AgentLifecycleState.WAITING])
        ),
        escaping_path,
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(host),
        step=UpgradeFailedStep.PREPARE,
        error_message=f"{escaping_path} is not a path inside the agent's state dir",
    )
    assert host.is_lock_held_at_stop == []
    assert not (state_dir.parent / "escaped-48213").exists()
    assert sorted(path.name for path in state_dir.iterdir()) == ["data.json"]


@pytest.mark.parametrize("is_dry_run", [True, False])
def test_upgrade_reports_an_input_that_is_not_utf8_text_as_a_prepare_failure(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext, is_dry_run: bool
) -> None:
    agent = _with_input_path(
        make_recorded_upgradable_agent(
            local_host, tmp_path, temp_mngr_ctx, {}, "echo new-48213\n", UpgradableCommandAgent
        ),
        Path("binary-input-48213"),
    )
    (get_agent_state_dir_path(local_host.host_dir, agent.id) / "binary-input-48213").write_bytes(b"\xff\xfe48213")

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=is_dry_run, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert isinstance(outcome, AgentUpgradeFailed)
    assert outcome.step == UpgradeFailedStep.PREPARE
    assert outcome.error_message.startswith("binary-input-48213 is not UTF-8 text: ")


_INPUT_A: Final[Path] = Path("a-59124.json")
_INPUT_B: Final[Path] = Path("nested") / "b-59124.sh"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        pytest.param(
            "---MNGR_UPGRADE_INPUT_PRESENT---\neyJrIjog\nMX0=\n\n---MNGR_UPGRADE_INPUT_MISSING---\n",
            {_INPUT_A: '{"k": 1}', _INPUT_B: None},
            id="wrapped_and_missing",
        ),
        pytest.param(
            "---MNGR_UPGRADE_INPUT_PRESENT---\n\n---MNGR_UPGRADE_INPUT_PRESENT---\nZWNobyDinJMK\n",
            {_INPUT_A: "", _INPUT_B: "echo \u2713\n"},
            id="empty_and_unicode",
        ),
        pytest.param("agent-record-missing\n", None, id="record_gone"),
    ],
)
def test_parse_upgrade_inputs_output_decodes_each_input_in_the_order_named(
    stdout: str, expected: dict[Path, str | None] | None
) -> None:
    assert parse_upgrade_inputs_output(stdout, (_INPUT_A, _INPUT_B)) == expected


def test_parse_upgrade_inputs_output_refuses_a_reply_that_lists_too_few_inputs() -> None:
    with pytest.raises(MngrError, match="^the host reported 1 of the agent's 2 upgrade inputs$"):
        parse_upgrade_inputs_output("---MNGR_UPGRADE_INPUT_MISSING---\n", (_INPUT_A, _INPUT_B))


def test_parse_upgrade_inputs_output_refuses_content_that_does_not_decode() -> None:
    with pytest.raises(MngrError, match=f"^the host sent {_INPUT_A} in a form that does not decode: "):
        parse_upgrade_inputs_output("---MNGR_UPGRADE_INPUT_PRESENT---\nnot base64!\n", (_INPUT_A,))


def test_parse_upgrade_inputs_output_refuses_an_input_that_is_not_utf8_text() -> None:
    with pytest.raises(InvalidUpgradeInputError, match=f"^{_INPUT_A} is not UTF-8 text: "):
        parse_upgrade_inputs_output("---MNGR_UPGRADE_INPUT_PRESENT---\n//4=\n", (_INPUT_A,))
