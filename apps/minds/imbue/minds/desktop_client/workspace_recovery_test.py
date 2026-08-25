"""Unit coverage for the workspace-recovery engine (passive verdicts + restart worker).

These exercise the building blocks behind ``POST /api/v1/workspaces/<id>/restart``
and the recovery card's polled verdicts directly, complementing the end-to-end
route tests in ``api_v1_test.py`` with the granular restart-sequence failure
modes (unresolved system-services agent, stop/start command failures, the
host-already-stopped fast path).
"""

import shlex
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.agent_creator import WORKSPACE_READY_TIMEOUT_SECONDS
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import ConnectivityFacet
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.environment_signals import SshEndpoint
from imbue.minds.desktop_client.mngr_command import OUTPUT_TAIL_MAX_CHARS
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import ManualClock
from imbue.minds.desktop_client.testing import PUBLIC_SSH_ENDPOINTS
from imbue.minds.desktop_client.testing import STUB_CONNECTIVITY_HOSTS
from imbue.minds.desktop_client.testing import SYSTEM_SERVICES_PROVIDER_NAME
from imbue.minds.desktop_client.testing import SeededAgent
from imbue.minds.desktop_client.testing import SideEffectingStubNetworkProber
from imbue.minds.desktop_client.testing import StubNetworkProber
from imbue.minds.desktop_client.testing import bring_stub_network_back
from imbue.minds.desktop_client.testing import bring_stub_network_up
from imbue.minds.desktop_client.testing import build_connectivity_detector_over
from imbue.minds.desktop_client.testing import build_resolver_with_provider_backend
from imbue.minds.desktop_client.testing import build_resolver_with_provider_backends
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector
from imbue.minds.desktop_client.testing import capture_error_logs
from imbue.minds.desktop_client.testing import record_provider_discovery_error
from imbue.minds.desktop_client.testing import scripted_workspace_probe_server
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_recovery import HOST_ACCESS_REJECTED_REASON
from imbue.minds.desktop_client.workspace_recovery import ProviderErrorConnectivityTrigger
from imbue.minds.desktop_client.workspace_recovery import RestartDispatchOutcome
from imbue.minds.desktop_client.workspace_recovery import RestartReadinessOutcome
from imbue.minds.desktop_client.workspace_recovery import UnattendedRecoveryDispatcher
from imbue.minds.desktop_client.workspace_recovery import WorkspaceSshEndpointSource
from imbue.minds.desktop_client.workspace_recovery import _HOST_RESTART_STARTUP_WAIT_SECONDS
from imbue.minds.desktop_client.workspace_recovery import _await_system_interface_ready
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_start_argv
from imbue.minds.desktop_client.workspace_recovery import _build_mngr_stop_argv
from imbue.minds.desktop_client.workspace_recovery import _build_restart_agent_address
from imbue.minds.desktop_client.workspace_recovery import _did_start_boot_a_host
from imbue.minds.desktop_client.workspace_recovery import _in_band_provider_outage_reason
from imbue.minds.desktop_client.workspace_recovery import _is_discovery_fresh
from imbue.minds.desktop_client.workspace_recovery import _provider_error_message_for_workspace
from imbue.minds.desktop_client.workspace_recovery import _report_restart_step_failure
from imbue.minds.desktop_client.workspace_recovery import _run_mngr
from imbue.minds.desktop_client.workspace_recovery import _run_mngr_capturing
from imbue.minds.desktop_client.workspace_recovery import dispatch_host_restart
from imbue.minds.desktop_client.workspace_recovery import is_network_dependent_workspace
from imbue.minds.desktop_client.workspace_recovery import is_recovery_classification_trustworthy
from imbue.minds.desktop_client.workspace_recovery import read_backend_unreachable_verdict
from imbue.minds.desktop_client.workspace_recovery import read_device_cannot_connect_verdict
from imbue.minds.desktop_client.workspace_recovery import run_restart_sequence
from imbue.minds.errors import MindError
from imbue.minds.errors import MngrCommandError
from imbue.minds.errors import MngrCommandTimeoutError
from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import PersistedProviderInstanceConfig
from imbue.mngr.api.find import _collect_required_provider_names
from imbue.mngr.errors import HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE
from imbue.mngr.errors import ProviderUnavailableError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

# Long enough that only a genuinely broken run reaches it. The wait ends the
# instant the command turns up, so this bounds a failure, not a passing test --
# and it is kept inside the suite's own ``--timeout=10`` per-test budget, so a
# broken run fails on the assertion rather than on pytest's opaque timeout.
_DISPATCH_WAIT_SECONDS: Final[float] = 5.0


def _write_mngr_stub(script: Path, subcommand_cases: str) -> str:
    """Write an executable stub that stands in for the ``mngr`` binary, and return its path.

    ``subcommand_cases`` are the ``case "$1"`` arms that give the stub whatever
    behaviour a test needs from it; every other subcommand exits cleanly. Every
    invocation appends its argv to a ``<script>.log`` sibling file, which
    :func:`_read_fake_mngr_invocations` reads back, so a test can assert which
    subcommands ran (e.g. that the stop step was skipped).
    """
    script.write_text('#!/bin/sh\necho "$@" >> "$0.log"\ncase "$1" in\n' + subcommand_cases + "  *) exit 0 ;;\nesac\n")
    script.chmod(0o755)
    return str(script)


def _write_fake_mngr(
    tmp_path: Path,
    stop_exit: int = 0,
    start_exit: int = 0,
    was_host_started: bool | None = None,
) -> str:
    """Write an mngr stub that exits per-subcommand.

    Lets a test simulate a failing stop or start without a real mngr / provider.
    ``was_host_started`` makes the start print the ``--format json`` result line
    real ``mngr start`` prints; None prints nothing, standing in for an older
    binary that cannot answer.
    """
    if was_host_started is None:
        start_case = f"  start) exit {start_exit} ;;\n"
    else:
        result = '{"started_agents": [], "count": 0, "was_host_started": %s}' % (
            "true" if was_host_started else "false"
        )
        start_case = f"  start) echo '{result}'; exit {start_exit} ;;\n"
    return _write_mngr_stub(tmp_path / "fake_mngr", f"  stop) exit {stop_exit} ;;\n" + start_case)


def _read_fake_mngr_invocations(mngr_binary: str) -> list[str]:
    """Return the recorded argv lines for a stub from :func:`_write_mngr_stub` (empty if never invoked)."""
    log_path = Path(mngr_binary + ".log")
    if not log_path.exists():
        return []
    return log_path.read_text().splitlines()


def _wait_for_mngr_invocation(mngr_binary: str, prefix: str) -> bool:
    """Wait for a dispatched restart worker to actually reach ``mngr <prefix>``.

    Must be called while the concurrency group is still open: the worker runs
    its mngr commands *through* that group, and the ``with`` exit flips it out
    of ACTIVE before joining the strands, so a worker not yet scheduled wakes to
    a group that refuses to run processes.
    """
    return poll_until(
        lambda: any(line.startswith(prefix) for line in _read_fake_mngr_invocations(mngr_binary)),
        timeout=_DISPATCH_WAIT_SECONDS,
        poll_interval=0.02,
    )


def _started_registry(workspace_agent: AgentId) -> InMemoryWorkspaceOperationRegistry:
    """A fresh operation registry with a RESTART operation already started for the agent."""
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    return registry


# -- argv builders --


def test_build_mngr_stop_argv_always_stops_the_host() -> None:
    """The restart is host-only (the surgical services tier is gone), so the stop
    always carries --stop-host."""
    aid = AgentId.generate()
    argv = _build_mngr_stop_argv("/usr/local/bin/mngr", str(aid))
    assert argv[:3] == ["/usr/local/bin/mngr", "stop", str(aid)]
    assert "--stop-host" in argv


def test_build_mngr_start_argv_targets_the_agent_and_asks_for_structured_output() -> None:
    """The start must be structured: its ``was_host_started`` is the only way to
    learn that an idempotent start booted nothing, which is what stops the
    terminal state from claiming a restart that never ran.

    It must also stay verbose. The two demands read as opposites and are not:
    ``--format json`` owns stdout (the one result line), while ``-v`` widens the
    logging that goes to stderr, which is the step timeline a killed-on-timeout
    start is diagnosed from. Reintroducing ``--quiet`` for the structured output
    would silence that timeline and put start timeouts back in the dark.
    """
    aid = AgentId.generate()
    argv = _build_mngr_start_argv("/usr/local/bin/mngr", str(aid))
    assert argv[:3] == ["/usr/local/bin/mngr", "start", str(aid)]
    assert argv[-2:] == ["--format", "json"]
    assert "-v" in argv
    assert "--quiet" not in argv


@pytest.mark.parametrize(
    "stdout, expected",
    [
        ('{"started_agents": [], "count": 0, "was_host_started": false}', False),
        ('{"started_agents": ["a"], "count": 1, "was_host_started": true}', True),
        # An older mngr on PATH, or output that is not the result line: absence
        # of evidence, which a caller must not fold into "nothing booted".
        ("Successfully started 1 agent(s)", None),
        ("", None),
        ('{"count": 0}', None),
        ("not json at all {", None),
    ],
)
def test_did_start_boot_a_host_reads_only_a_real_answer(stdout: str, expected: bool | None) -> None:
    assert _did_start_boot_a_host(stdout) is expected


def test_an_unreadable_start_result_is_reported_rather_than_passed_over() -> None:
    """Silence disables the no-op detection for every restart, so it cannot be silent itself."""
    with capture_loguru(level="WARNING") as log_output:
        assert _did_start_boot_a_host("Successfully started 1 agent(s)") is None
    # Human output is not JSON, so this is the decode branch specifically -- the
    # other warning arm is for output that parsed but carried no field.
    assert "Could not read" in log_output.getvalue()


def test_restart_agent_address_restricts_discovery_to_one_provider() -> None:
    """The pinned address must be one ``mngr`` reads as naming a single provider.

    Asserted through mngr's own parser rather than against a literal, because the
    property that matters is the one ``find_all_agents`` acts on: a provider
    filter of exactly this agent's provider, so a restart never pays for -- or
    fails on -- a provider that could not have hosted it.
    """
    services_agent = AgentId.generate()
    host_id = HostId.generate()
    address = _build_restart_agent_address(
        services_agent,
        AgentDisplayInfo(agent_name="workspace", host_id=str(host_id), provider_name="imbue_cloud_gabriel-imbue-com"),
    )

    parsed = parse_agent_address(address)
    assert parsed.agent == services_agent
    assert _collect_required_provider_names([parsed]) == (ProviderInstanceName("imbue_cloud_gabriel-imbue-com"),)


@pytest.mark.parametrize(
    "display_info",
    [
        pytest.param(None, id="no-discovery-row"),
        pytest.param(AgentDisplayInfo(agent_name="w", host_id="host-" + "0" * 32), id="no-provider"),
        pytest.param(
            AgentDisplayInfo(agent_name="w", host_id="localhost", provider_name="local"), id="placeholder-host"
        ),
    ],
)
def test_restart_agent_address_falls_back_to_the_bare_id(display_info: AgentDisplayInfo | None) -> None:
    """A coordinate discovery cannot supply costs the scoping, never the restart."""
    services_agent = AgentId.generate()
    assert _build_restart_agent_address(services_agent, display_info) == str(services_agent)


def test_run_restart_sequence_pins_the_provider_on_both_steps(tmp_path: Path) -> None:
    """Both subprocesses address the machine by provider, not by bare agent id.

    The stop matters as much as the start: it runs the same unpinned discovery
    and is just as able to fail on an unrelated provider.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    host_id = HostId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, host_id=host_id)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    expected = f"{services_agent}@{host_id}.{SYSTEM_SERVICES_PROVIDER_NAME}"
    invocations = _read_fake_mngr_invocations(mngr_binary)
    assert [line.split()[1] for line in invocations] == [expected, expected]


# -- timed-out subprocess output capture --


def test_run_mngr_capturing_timeout_carries_the_output_tail(tmp_path: Path) -> None:
    """A timed-out mngr subprocess's captured output rides the error instead of being discarded.

    The tail is the only record of which step the killed command died in; the
    message itself stays short because it is what user-facing surfaces render.
    """
    script = tmp_path / "hanging_mngr"
    script.write_text("#!/bin/sh\necho step-one-done\necho step-two-started >&2\nsleep 30\n")
    script.chmod(0o755)

    caught: MngrCommandTimeoutError | None = None
    with ConcurrencyGroup(name="test-timeout-tail") as cg:
        try:
            _run_mngr_capturing(cg, [str(script), "start", "agent-x"], env={}, timeout_seconds=2.0)
        except MngrCommandTimeoutError as exc:
            caught = exc

    assert caught is not None
    assert "step-one-done" in (caught.output_tail or "")
    assert "step-two-started" in (caught.output_tail or "")
    assert "step-one-done" not in str(caught)


def test_restart_step_failure_logs_the_timeout_output_tail_without_widening_the_user_message() -> None:
    """The timeout's output tail reaches the (single) error record but not the user-facing message."""
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()
    exc = MngrCommandTimeoutError(
        "timed out after 1260s",
        output_tail="--- stderr tail ---\nacquiring host lock at /home/user/.mngr/host_lock",
    )

    with capture_error_logs() as error_records:
        _report_restart_step_failure(
            "Start",
            exc,
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            registry=_started_registry(workspace_agent),
            connectivity_detector=None,
        )

    assert len(error_records) == 1, error_records
    assert "acquiring host lock" in error_records[0]
    message = tracker.get_last_restart_error(workspace_agent) or ""
    assert "acquiring host lock" not in message


def _run_failing_mngr_stub(cg: ConcurrencyGroup, script: Path, stderr_script_body: str) -> MngrCommandError:
    """Run a stub that writes ``stderr_script_body`` to stderr and exits 1, returning the raised error."""
    script.write_text(f"#!/bin/sh\n{stderr_script_body}exit 1\n")
    script.chmod(0o755)
    with pytest.raises(MngrCommandError) as exc_info:
        _run_mngr(cg, [str(script), "stop", "agent-x"], env={})
    return exc_info.value


def test_run_mngr_narrows_a_nonzero_exit_error_to_mngrs_verdict(tmp_path: Path) -> None:
    """A failed command's error message is mngr's verdict; the timeline it printed rides the tail.

    With ``-v`` in the recovery argv the captured stderr is the whole DEBUG
    timeline. This message is rendered to the user and is what the substring
    consumers (the shutdown-not-supported match, provider outage parsing) key
    on, so only the verdict block may reach it. The whole block does: a verdict
    spans lines, since mngr appends its bracketed help text.
    """
    with ConcurrencyGroup(name="test-verdict-only") as cg:
        caught = _run_failing_mngr_stub(
            cg,
            tmp_path / "failing_mngr",
            'i=0; while [ $i -lt 500 ]; do echo "DEBUG step $i" >&2; i=$((i+1)); done\n'
            "echo 'Error: the real failure' >&2\n"
            "echo '  [try it the other way]' >&2\n",
        )

    assert str(caught) == "exited 1: Error: the real failure\n  [try it the other way]"
    assert len(str(caught)) <= len("exited 1: ") + OUTPUT_TAIL_MAX_CHARS
    assert "DEBUG step 499" in (caught.output_tail or "")


def test_run_mngr_keeps_a_tolerated_provider_skip_out_of_the_verdict(tmp_path: Path) -> None:
    """A provider mngr skipped and carried on past is not read as this machine's backend outage.

    Under ``-v``, mngr logs every provider it skips as unavailable at DEBUG with
    the verbatim ``ProviderUnavailableError`` text that the outage parser
    matches -- for a provider it then *continued past*. Letting that into the
    error message would report a step that died of something else entirely as a
    backend outage, and record it stickily on the tracker for the episode.
    """
    provider = ProviderInstanceName("imbue_cloud_someone-imbue-com")
    skip_reason = "could not reach Imbue Cloud: [Errno 8] nodename nor servname provided"
    stderr_path = tmp_path / "skip_then_fail.txt"
    stderr_path.write_text(
        f"Skipping provider {provider} (unavailable): {ProviderUnavailableError(provider, skip_reason)}\n"
        "Error: Agent agent-x not found\n"
    )

    with ConcurrencyGroup(name="test-tolerated-skip") as cg:
        caught = _run_failing_mngr_stub(
            cg, tmp_path / "failing_mngr_with_skip", f"cat {shlex.quote(str(stderr_path))} >&2\n"
        )

    assert _in_band_provider_outage_reason(caught, str(provider)) is None
    assert str(caught) == "exited 1: Error: Agent agent-x not found"
    assert skip_reason in (caught.output_tail or "")


def test_run_mngr_falls_back_to_the_stderr_tail_when_mngr_printed_no_verdict(tmp_path: Path) -> None:
    """An mngr that crashed without a verdict still reports whatever it did print.

    An unhandled exception reaches stderr as a traceback with no ``Error:``
    marker, and that traceback is the only diagnosis there is -- so the message
    must not come out empty.
    """
    with ConcurrencyGroup(name="test-verdict-fallback") as cg:
        caught = _run_failing_mngr_stub(
            cg,
            tmp_path / "crashing_mngr",
            "echo 'Traceback (most recent call last):' >&2\necho 'RecursionError' >&2\n",
        )

    assert "RecursionError" in str(caught)


# -- provider-error attribution --


def test_provider_error_message_for_workspace_keys_on_this_workspaces_provider() -> None:
    """The provider error message is attributed by exact provider name.

    This is the per-provider keying that keeps a docker mind's recovery from
    being misclassified during a simultaneous imbue_cloud outage: only an error
    whose provider name matches this machine's is used.
    """
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="could not reach Imbue Cloud",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    matched = _provider_error_message_for_workspace(errors, "imbue_cloud_acme", True)
    assert matched == "could not reach Imbue Cloud"
    # The same error is withheld while no snapshot at/after the outage onset
    # backs it: it is a property of one snapshot, like the host state.
    assert _provider_error_message_for_workspace(errors, "imbue_cloud_acme", False) is None


def test_provider_error_message_for_workspace_ignores_other_providers() -> None:
    """An error for a different provider is never blamed on this machine."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker", True) is None


def test_provider_error_message_for_workspace_is_none_when_provider_unknown() -> None:
    """Pre-discovery (provider unknown), we cannot attribute any error to this machine."""
    errors = {
        ProviderInstanceName("imbue_cloud_acme"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message="down",
            provider_name=ProviderInstanceName("imbue_cloud_acme"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, None, True) is None


def test_provider_error_message_for_workspace_reduces_the_generic_shape_to_its_reason() -> None:
    """A snapshot carrying mngr's whole generic message yields just the reason.

    Discovery records an error's ``__cause__`` when it has one, so a provider
    that raised ``ProviderUnavailableError`` without a ``from`` clause (docker's
    stopped-state-container check) surfaces the wrapper where its neighbours
    surface a bare sentence. The card shows this under its own
    "Can't connect to Docker" heading, so the wrapper would name the provider a
    second time and trail mngr's internal marker sentence at the user.
    """
    reason = "Docker state container is stopped; host records are unreachable"
    errors = {
        ProviderInstanceName("docker"): DiscoveryError(
            type_name="ProviderUnavailableError",
            message=str(ProviderUnavailableError(ProviderInstanceName("docker"), reason)),
            provider_name=ProviderInstanceName("docker"),
        ),
    }
    assert _provider_error_message_for_workspace(errors, "docker", True) == reason


# -- restart worker --


def test_run_restart_sequence_fails_when_system_services_agent_is_unresolved(tmp_path: Path) -> None:
    """With no system-services agent discovered, the sequence ends in RESTART_FAILED."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=MngrCliBackendResolver(),
            mngr_binary="mngr",
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "system-services" in (tracker.get_last_restart_error(workspace_agent) or "")
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.FAILED
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_stop_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr stop`` ends the sequence in RESTART_FAILED naming the stop step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, stop_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_start_command_errors(tmp_path: Path) -> None:
    """A non-zero ``mngr start`` ends the sequence in RESTART_FAILED naming the start step."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, start_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Start step" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_and_reports_when_interface_never_answers(tmp_path: Path) -> None:
    """A clean stop+start whose interface never answers ends in RESTART_FAILED with one error log.

    With a plugin route configured (nonzero forward port + cookie) but nothing
    answering on it, the readiness wait times out; this failure branch was
    previously unlogged, so pin that it now reports exactly once.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            # Port 1 refuses connections, so every readiness poll fails fast.
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "did not respond" in (tracker.get_last_restart_error(workspace_agent) or "")
    assert len(error_records) == 1, error_records


def test_run_restart_sequence_fails_when_stop_command_cannot_launch(tmp_path: Path) -> None:
    """A launch failure (missing ``mngr`` binary) surfaces as RESTART_FAILED naming the stop step.

    Exercises the path where ``_run_mngr`` wraps the ``OSError`` from the failed
    fork/exec into a ``MngrCommandError`` and the restart sequence catches that
    single domain error at the call site.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    missing_binary = str(tmp_path / "definitely_not_a_real_mngr")

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=missing_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert "Stop step" in (tracker.get_last_restart_error(workspace_agent) or "")


def test_run_restart_sequence_recovers_on_clean_dispatch_without_plugin(tmp_path: Path) -> None:
    """Clean stop+start with no plugin route to probe through recovers the agent to HEALTHY."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def test_run_restart_sequence_skips_unsupported_stop_and_proceeds(tmp_path: Path) -> None:
    """A host-restart on a provider that cannot stop a host in place (Modal: ``mngr stop
    --stop-host`` raises HostShutdownNotSupportedError) must NOT fail the restart -- the stop
    step is skipped and the sequence proceeds to ``mngr start``, which restarts it on its own."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)
    # A fake mngr whose ``stop`` fails with the host-shutdown-not-supported message (as Modal
    # does) and whose ``start`` succeeds -- mirrors a no-shutdown provider's restart. The stderr
    # is built from mngr's exported HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE, the same constant the
    # restart worker matches on, so this exercises the real shared-source-of-truth mechanism.
    script = tmp_path / "fake_mngr_no_shutdown"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  stop) echo "Provider modal {HOST_SHUTDOWN_NOT_SUPPORTED_MESSAGE}" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(0o755)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=str(script),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
        )

    # The unsupported stop is treated as "skip and proceed", so the restart recovers (not FAILED).
    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE


def test_run_restart_sequence_skips_stop_for_start_only_dispatch(tmp_path: Path) -> None:
    """``skip_stop=True`` (the API's ``start_only``) goes straight to ``mngr start`` (no stop subprocess)."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
            skip_stop=True,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    assert any(line.startswith("start ") for line in invocations)
    assert not any(line.startswith("stop ") for line in invocations)


def test_run_restart_sequence_stops_before_start_by_default(tmp_path: Path) -> None:
    """Without ``skip_stop``, a host restart stops the host before starting it."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)

    with ConcurrencyGroup(name="test-restart") as cg:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    invocations = _read_fake_mngr_invocations(mngr_binary)
    stop_index = next((i for i, line in enumerate(invocations) if line.startswith("stop ")), None)
    start_index = next((i for i, line in enumerate(invocations) if line.startswith("start ")), None)
    assert stop_index is not None, invocations
    assert start_index is not None, invocations
    assert stop_index < start_index


# -- recovery classification trustworthiness (freshness gate on the verdict path) --


def _drive_to_stuck_with_onset(tracker: SystemInterfaceHealthTracker, agent_id: AgentId) -> datetime:
    """Drive ``agent_id`` to STUCK via the real probe path and return its onset.

    A zero stuck-threshold makes the first probe failure stick immediately, so the
    outage onset is recorded deterministically without sleeping.
    """
    tracker.record_failure(agent_id)
    tracker.record_probe_failure(agent_id)
    assert tracker.get_health(agent_id) == AgentHealth.STUCK
    onset = tracker.get_failure_run_started_wall_at(agent_id)
    assert onset is not None
    return onset


def _register_workspace_agent(resolver: MngrCliBackendResolver, agent_id: AgentId, provider_name: str) -> None:
    """Register one machine agent on ``provider_name`` so its display info resolves a provider.

    Trustworthiness is scoped to the machine's own provider's last snapshot, so
    the agent must be discoverable with a provider for the predicate to find a
    per-provider snapshot time.
    """
    agent = DiscoveredAgent(
        host_id=HostId("host-" + "0" * 31 + "1"),
        agent_id=agent_id,
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": {"workspace": "true", "is_primary": "true"}},
    )
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent_id,), discovered_agents=(agent,)))


def _set_provider_snapshot_at(resolver: MngrCliBackendResolver, provider_name: str, snapshot_at: datetime) -> None:
    """Record ``provider_name``'s last per-provider snapshot time on the resolver."""
    resolver.update_providers(
        provider_name=ProviderInstanceName(provider_name),
        provider=None,
        error=None,
        last_snapshot_at=snapshot_at,
    )


def test_is_discovery_fresh_distinguishes_recent_from_stale_and_missing() -> None:
    """Only a recent snapshot backs a trustworthy classification via the age fallback."""
    now = datetime.now(timezone.utc)
    assert _is_discovery_fresh(now, 10.0) is True
    # A snapshot well past the freshness window (a stalled pipeline) is stale.
    assert _is_discovery_fresh(now - timedelta(minutes=5), 10.0) is False
    # No snapshot at all (e.g. before initial discovery) cannot be trusted.
    assert _is_discovery_fresh(None, 10.0) is False


def test_discovery_freshness_is_measured_against_the_providers_own_cadence() -> None:
    """A provider that re-polls slowly is not stale merely for polling slowly.

    The window is a multiple of the cadence of the loop that produced the
    snapshot. Aging a 30s-cadence provider against the 10s stream baseline would
    call it stale one baseline-window after each of its perfectly normal polls.
    """
    aged = datetime.now(timezone.utc) - timedelta(seconds=45)
    assert _is_discovery_fresh(aged, 10.0) is False
    assert _is_discovery_fresh(aged, 30.0) is True


def test_classification_trustworthy_only_after_a_post_onset_snapshot() -> None:
    """A verdict is trustworthy only once a snapshot taken *after* the outage began has landed.

    A snapshot that predates the outage still carries the pre-outage host state (a
    just-stopped container still reads RUNNING), so it must not make the
    classification trustworthy -- only a snapshot at or after the outage onset
    does. Freshness is scoped to the machine's own provider's snapshot time.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    onset = _drive_to_stuck_with_onset(tracker, agent_id)

    # A recent snapshot of the agent's provider that nonetheless predates the outage
    # is the exact bug case: within the absolute freshness window but still showing
    # the pre-outage host state, so the classification stays untrustworthy.
    _set_provider_snapshot_at(resolver, "docker", onset - timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False

    # A snapshot of the agent's provider taken after the outage began reflects it.
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True


def test_classification_trustworthiness_is_scoped_to_the_workspaces_own_provider() -> None:
    """A fresh snapshot of an *unrelated* provider must not make the verdict trustworthy.

    Each provider is discovered on its own loop, so only the machine's own
    provider's snapshot can establish that its host was re-observed post-onset.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    onset = _drive_to_stuck_with_onset(tracker, agent_id)

    # A post-onset snapshot for a different provider leaves docker's freshness stale.
    _set_provider_snapshot_at(resolver, "modal", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False

    # Only the agent's own provider going fresh post-onset makes it trustworthy.
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True


def test_classification_trustworthiness_without_onset_falls_back_to_age() -> None:
    """Without a recorded onset, trustworthiness falls back to the absolute-age freshness check.

    Only the force-``mark_stuck`` path (used in tests) reaches STUCK without a
    probe-failure run, so there is no onset to compare against; the predicate then
    behaves on age alone -- cold start is untrustworthy, a recent snapshot is
    trustworthy. A missing tracker is treated the same way.
    """
    resolver = MngrCliBackendResolver()
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId.generate()
    _register_workspace_agent(resolver, agent_id, "docker")
    tracker.mark_stuck(agent_id)
    assert tracker.get_failure_run_started_wall_at(agent_id) is None

    # Cold start, no snapshot yet: untrustworthy.
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False
    # A recent snapshot of the agent's provider is trustworthy via the age fallback.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is True
    # A stale snapshot (a stalled pipeline) is untrustworthy again.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc) - timedelta(minutes=5))
    assert is_recovery_classification_trustworthy(resolver, tracker, agent_id) is False
    # No tracker at all behaves identically to a missing onset.
    assert is_recovery_classification_trustworthy(resolver, None, agent_id) is False


# -- unattended recovery + the shared dispatch --


def _dispatcher(
    tracker: SystemInterfaceHealthTracker,
    resolver: MngrCliBackendResolver,
    registry: InMemoryWorkspaceOperationRegistry,
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    mngr_host_dir: Path,
    connectivity_detector: ConnectivityDetector | None = None,
) -> UnattendedRecoveryDispatcher:
    """The tracker callback wired in ``app.py``, built against test doubles."""
    return UnattendedRecoveryDispatcher(
        tracker=tracker,
        backend_resolver=resolver,
        registry=registry,
        concurrency_group=concurrency_group,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir,
        mngr_forward_port=0,
        mngr_forward_preauth_cookie=None,
        connectivity_detector=connectivity_detector,
    )


def test_unattended_recovery_starts_a_wedged_machine_without_bouncing_it(tmp_path: Path) -> None:
    """The STUCK edge dispatches a start-only restart: ``mngr start`` runs, ``mngr stop`` does not.

    start_only is what makes it safe to fire unprompted -- it can cold-boot a
    stopped host but can never bounce a live one.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "the unattended dispatch must reach mngr start"
    assert not any(line.startswith("stop ") for line in _read_fake_mngr_invocations(mngr_binary))


def test_unattended_recovery_leaves_a_machine_the_user_stopped_alone(tmp_path: Path) -> None:
    """An in-app stop suppresses the dispatch, so it cannot undo the user's action.

    A stopped host's interface is unreachable, so the probe loop drives it
    STUCK exactly as a wedge would. Without the marker the app would start it
    straight back up -- and bill for it on a metered provider.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    tracker.suppress_unattended_recovery(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_unattended_recovery_resumes_after_the_user_starts_the_machine_again(tmp_path: Path) -> None:
    """Starting from inside the app clears the suppression, so wedges self-heal again."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    tracker.suppress_unattended_recovery(workspace_agent)
    tracker.allow_unattended_recovery(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path)
        dispatch(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "clearing the suppression must let a wedge reach mngr start again"


def test_a_forced_stuck_does_not_re_dispatch(tmp_path: Path) -> None:
    """Only the probe-confirmed HEALTHY -> STUCK edge dispatches, once per episode.

    A ``mark_stuck`` and the probe failures that keep arriving while the
    machine stays down re-report the state, but the edge has already fired;
    a dispatcher keyed on the state instead would restart on every lap.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert _wait_for_mngr_invocation(mngr_binary, "start "), "the outage edge must reach mngr start"
        started_count = len(_read_fake_mngr_invocations(mngr_binary))
        tracker.mark_stuck(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

    assert len(_read_fake_mngr_invocations(mngr_binary)) == started_count


def test_dispatch_host_restart_does_not_stack_a_second_worker(tmp_path: Path) -> None:
    """A restart already in flight reports ALREADY_RUNNING instead of racing the first."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = InMemoryWorkspaceOperationRegistry()
    # The claim the first caller already won: the workspace's operation slot,
    # which is what owning a restart means.
    registry.start(workspace_agent, WorkspaceOperationKind.RESTART, datetime.now(timezone.utc))
    tracker.mark_restarting(workspace_agent, start_only=True)

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=True,
        )

    assert outcome == RestartDispatchOutcome.ALREADY_RUNNING


def test_probe_failures_alone_drive_a_wedged_machine_back_up(tmp_path: Path) -> None:
    """End to end through the real wiring: probe failures -> STUCK -> ``mngr start``.

    The tracker is wired exactly as ``app.py`` wires it and then driven only by
    probe results, so nothing but sustained unreachability triggers the restart.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "sustained probe failure must reach mngr start without anyone asking"
    assert not any(line.startswith("stop ") for line in _read_fake_mngr_invocations(mngr_binary))


def test_unattended_recovery_never_takes_a_backup_operation_s_slot(tmp_path: Path) -> None:
    """A restore stops the machine's services, so the wedge it produces must not evict it.

    ``registry.start`` replaces the workspace's record, so a restart that
    claimed the slot mid-restore would strand the restore's poller and let the
    restore worker's terminal complete/fail land on the restart's record --
    reporting a restart that never ran as done.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.BACKUP_RESTORE, datetime.now(timezone.utc))

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []
    record = registry.get(workspace_agent)
    assert record is not None and record.kind == WorkspaceOperationKind.BACKUP_RESTORE
    assert record.status == WorkspaceOperationStatus.RUNNING
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_dispatch_host_restart_reports_a_conflicting_operation(tmp_path: Path) -> None:
    """The route turns this outcome into its 409, so the enum has to name the case."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = InMemoryWorkspaceOperationRegistry()
    registry.start(workspace_agent, WorkspaceOperationKind.BACKUP_UPDATE, datetime.now(timezone.utc))

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=False,
        )

    assert outcome == RestartDispatchOutcome.OPERATION_CONFLICT


class _RegistryWithSlotStolenMidClaim(InMemoryWorkspaceOperationRegistry):
    """A registry where a backup operation claims the slot during the restart's own claim.

    Stands in for the interleaving a non-atomic claim would lose to: a backup
    dispatch claiming from a request thread, against a restart dispatched from
    the probe thread.
    """

    _thief_agent_id: AgentId | None = PrivateAttr(default=None)

    def steal_slot_on_next_claim(self, agent_id: AgentId) -> None:
        self._thief_agent_id = agent_id

    def start_if_idle(
        self, agent_id: AgentId, kind: WorkspaceOperationKind, now: datetime, target: str | None
    ) -> bool:
        if self._thief_agent_id == agent_id:
            self._thief_agent_id = None
            super().start(agent_id, WorkspaceOperationKind.BACKUP_RESTORE, now)
        return super().start_if_idle(agent_id, kind, now, target)


def test_a_backup_that_claims_the_slot_first_is_not_evicted_by_the_restart(tmp_path: Path) -> None:
    """The restart must lose the slot it did not win, not overwrite the winner's record.

    ``registry.start`` *replaces* the record, so a restart that read an idle
    slot and then filled it unconditionally would strand the restore's poller
    and let the restore worker's terminal complete/fail land on the restart's
    record -- reporting a restart that never ran as done. One atomic claim is
    what closes that window, so this drives a backup into the middle of it.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = _RegistryWithSlotStolenMidClaim()
    registry.steal_slot_on_next_claim(workspace_agent)

    with ConcurrencyGroup(name="test-unattended") as cg:
        outcome = dispatch_host_restart(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            registry=registry,
            concurrency_group=cg,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            skip_stop=True,
        )

    assert outcome == RestartDispatchOutcome.OPERATION_CONFLICT
    record = registry.get(workspace_agent)
    assert record is not None and record.kind == WorkspaceOperationKind.BACKUP_RESTORE
    assert record.status == WorkspaceOperationStatus.RUNNING
    # Nothing of the restart may survive its lost claim: no worker, and no
    # RESTARTING the recovery surfaces would render over the restore.
    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert tracker.get_health(workspace_agent) != AgentHealth.RESTARTING


def test_a_machine_that_answers_is_never_restarted(tmp_path: Path) -> None:
    """The other half of the edge: a reachable machine stays untouched."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    with ConcurrencyGroup(name="test-unattended") as cg:
        tracker.add_on_stuck_edge_callback(_dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path))
        tracker.record_failure(workspace_agent)
        tracker.record_probe_success(workspace_agent)

    assert _read_fake_mngr_invocations(mngr_binary) == []


# -- backend-unreachable verdict (the poll-cheap read) --


def _write_fake_mngr_with_provider_outage(
    tmp_path: Path, failing_subcommand: str, provider_name: str = "docker"
) -> tuple[str, str]:
    """Write an mngr stub whose ``failing_subcommand`` fails the way an unreachable backend does.

    Returns ``(binary_path, reason)``. The stderr is rendered from a real
    ``ProviderUnavailableError`` -- exactly what mngr prints when it cannot reach
    the provider -- so this exercises the shared message shape rather than a
    hand-copied literal. The message is written to a sibling file and ``cat``ed so
    the quotes it embeds around the provider name survive the shell stub.

    ``provider_name`` is the provider the command fails at, which defaults to the
    one the test resolvers put the workspace on. A command aborts on whichever
    provider it queried is unavailable, so passing another one is how a test
    poses an outage that is not this machine's.
    """
    reason = "could not reach the provider backend: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName(provider_name), reason)
    message_path = tmp_path / f"provider_outage_{failing_subcommand}_{provider_name}.txt"
    message_path.write_text(f"Error: {error}\n")
    script = tmp_path / f"fake_mngr_provider_outage_{failing_subcommand}_{provider_name}"
    mngr_binary = _write_mngr_stub(
        script, f"  {failing_subcommand}) cat {shlex.quote(str(message_path))} >&2; exit 1 ;;\n"
    )
    return mngr_binary, reason


def test_in_band_provider_outage_answers_only_for_a_known_matching_provider() -> None:
    """A command rejected at some other provider is not this machine's backend going down.

    The unknown-provider case is the one that matters: mngr aborts on whichever
    provider it queried turns out to be unavailable, so a caller that could not
    resolve its own provider (discovery lost the host across the restart, or has
    not surfaced it yet) has nothing to compare against -- and adopting the
    outage anyway would withhold a restart from a machine whose backend is fine.
    """
    reason = "could not reach Imbue Cloud: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName("imbue_cloud_someone-imbue-com"), reason)
    exc = MngrCommandError(f"exited 1: Error: {error}")

    assert _in_band_provider_outage_reason(exc, "imbue_cloud_someone-imbue-com") == reason
    assert _in_band_provider_outage_reason(exc, "docker") is None
    assert _in_band_provider_outage_reason(exc, None) is None


def test_restart_step_failure_names_the_step_when_the_machines_provider_is_unknown() -> None:
    """A restart step that failed at an unidentifiable provider stays a failed step.

    The stop step can drop the host out of discovery, so the start step's failure
    is read with no display info to resolve a provider from. Reporting the foreign
    outage the stderr happens to name would tell the user their machine's backend
    is down when it is another account's cloud that is -- and would leave that
    claim on the tracker for the rest of the episode.
    """
    reason = "could not reach Imbue Cloud: [Errno 8] nodename nor servname provided, or not known"
    error = ProviderUnavailableError(ProviderInstanceName("imbue_cloud_someone-imbue-com"), reason)
    exc = MngrCommandError(f"exited 1: Error: {error}")
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()

    with capture_error_logs():
        _report_restart_step_failure(
            "Start",
            exc,
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
            registry=_started_registry(workspace_agent),
            connectivity_detector=None,
        )

    message = tracker.get_last_restart_error(workspace_agent) or ""
    assert message.startswith("Start step of host restart failed:")
    assert "This machine's backend is unreachable" not in message
    assert tracker.get_backend_outage(workspace_agent) is None


def test_backend_unreachable_verdict_surfaces_the_providers_own_error() -> None:
    """A surfaced provider error is the verdict, carrying that provider's own words.

    The message is shown verbatim precisely so minds never has to hand-author a
    sentence per provider failure mode.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    record_provider_discovery_error(
        resolver, "docker", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=None)

    assert verdict is not None
    assert verdict.provider_label == "Docker"
    assert verdict.reason == "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


def test_backend_unreachable_verdict_withholds_a_provider_error_from_a_previous_episode() -> None:
    """A provider error latched before this outage began must not explain it.

    ``get_provider_errors()`` holds a provider's last reported error until its
    next poll lands, so a backend that failed and then recovered keeps an error
    on the books for up to a full poll interval. A machine that wedges in that
    window (for reasons entirely its own) would otherwise be reported as
    "Can't connect to Docker" on the strength of a snapshot taken before it
    wedged -- naming a backend that is, by then, answering fine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    reason = "Docker Desktop is manually paused."
    record_provider_discovery_error(resolver, "docker", reason, last_snapshot_at=onset - timedelta(seconds=1))

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None

    # The next poll of that provider settles it: if the backend really is down,
    # its error is now an observation of *this* outage and speaks again.
    record_provider_discovery_error(resolver, "docker", reason, last_snapshot_at=onset + timedelta(seconds=1))
    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.reason == reason


def test_backend_unreachable_verdict_is_none_while_the_backend_answers() -> None:
    """No provider error and a reachable host is not a verdict this read can make.

    This read only ever answers "is the backend unreachable?", and a wedged but
    reachable container looks identical to a healthy one here -- it is the probe
    loop that settles that -- so silence must not be mistaken for a healthy
    machine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, host_state=HostState.RUNNING)

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=None) is None


def test_backend_unreachable_verdict_covers_a_trusted_rejected_host() -> None:
    """A trustworthily UNAUTHENTICATED host is a backend verdict, with the canned reason.

    Discovery carries no per-host failure detail for this state, and a restart
    routes through the same rejected credential -- which is exactly why it must
    not read as an ordinary unresponsive machine.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent, services_agent, host_state=HostState.UNAUTHENTICATED
    )
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    _set_provider_snapshot_at(resolver, "docker", onset + timedelta(seconds=1))

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)

    assert verdict is not None
    assert verdict.reason == HOST_ACCESS_REJECTED_REASON


def test_backend_unreachable_verdict_withholds_an_untrusted_rejected_host() -> None:
    """A pre-onset snapshot cannot carry the rejected-host verdict.

    Same freshness gate the probe's host-state verdicts use: this state steers
    the card away from offering a restart, so a stale reading must not.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent, services_agent, host_state=HostState.UNAUTHENTICATED
    )
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    onset = _drive_to_stuck_with_onset(tracker, workspace_agent)
    _set_provider_snapshot_at(resolver, "docker", onset - timedelta(seconds=1))

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None


def test_run_restart_sequence_reports_the_backend_when_the_start_is_rejected_there(tmp_path: Path) -> None:
    """A start that mngr rejected at the provider is reported as a backend outage.

    The command never reached the host, so naming the start step would blame the
    machine for its backend being down -- and send the reader looking in the
    wrong place. The provider's own reason is surfaced instead.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=False)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    mngr_binary, reason = _write_fake_mngr_with_provider_outage(tmp_path, "start")

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    message = tracker.get_last_restart_error(workspace_agent) or ""
    assert reason in message
    assert "Start step" not in message
    assert len(error_records) == 1, error_records


def _run_restart_rejected_at_the_backend(
    tmp_path: Path, workspace_agent: AgentId, tracker: SystemInterfaceHealthTracker, resolver: MngrCliBackendResolver
) -> str:
    """Run the restart the tracker's stuck edge dispatches, against a backend that refuses it.

    Returns the provider's own reason. This is the sequence the app runs
    unattended the moment a machine wedges, so it is what has happened by the
    time any recovery surface is on screen.
    """
    mngr_binary, reason = _write_fake_mngr_with_provider_outage(tmp_path, "start")
    with ConcurrencyGroup(name="test-restart-rejected") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=mngr_binary,
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=_started_registry(workspace_agent),
            skip_stop=True,
        )
    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    return reason


def test_a_restart_rejected_at_the_backend_is_the_verdict_without_waiting_for_a_poll(tmp_path: Path) -> None:
    """The rejection names the backend on the edge that raises the card, not a poll later.

    Discovery has surfaced nothing about this provider -- the outage is seconds
    old, and its next poll can be half a minute away. The rejected restart is the
    only observation there is, and it is the same observation discovery will
    eventually make, so the card opens on "Can't connect to Docker" instead of
    opening on "unresponsive" with a Restart button routed through the backend
    that just refused one, and correcting itself while the user reads it.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate(), host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    _drive_to_stuck_with_onset(tracker, workspace_agent)

    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None

    reason = _run_restart_rejected_at_the_backend(tmp_path, workspace_agent, tracker, resolver)

    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.provider_label == "Docker"
    assert verdict.reason == reason


def test_a_rejected_restarts_verdict_lasts_until_the_backend_is_next_polled(tmp_path: Path) -> None:
    """The next poll of that provider settles it, whichever way it reads.

    A rejection is only the freshest word on the backend until discovery gets
    one of its own. A poll that clears the provider must therefore end the
    claim: the backend is answering again, and a machine still wedged after that
    is wedged for reasons of its own -- reporting it as "Can't connect to Docker"
    for the rest of the episode would be the same stale-evidence mistake the
    freshness gate exists to prevent, made from the other side. A poll that
    still errors keeps the verdict, now on the resolver's own evidence.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate(), host_state=HostState.RUNNING)
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    _drive_to_stuck_with_onset(tracker, workspace_agent)
    reason = _run_restart_rejected_at_the_backend(tmp_path, workspace_agent, tracker, resolver)

    # Still down when discovery next looks: the same verdict, now off the poll.
    record_provider_discovery_error(resolver, "docker", reason)
    verdict = read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker)
    assert verdict is not None
    assert verdict.reason == reason

    # Back up when discovery next looks: nothing left for the rejection to say.
    _set_provider_snapshot_at(resolver, "docker", datetime.now(timezone.utc))
    assert read_backend_unreachable_verdict(workspace_agent, backend_resolver=resolver, tracker=tracker) is None


# -- the this-device-cannot-connect verdict --


@pytest.mark.parametrize(
    "reason",
    [
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        SystemInterfaceBackendFailureReason.POOL_EXHAUSTED,
    ],
)
def test_device_verdict_covers_every_failure_raised_before_the_backend_was_dialed(
    reason: SystemInterfaceBackendFailureReason,
) -> None:
    """Both causes that never reached the network read as this device's fault.

    They are not separated for the user: an app restart is the remedy for both
    and the only one available, so a second card would be a distinction with no
    action behind it. The verbatim error travels so the card can show what
    actually broke.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(workspace_agent, reason, "the exact error")

    verdict = read_device_cannot_connect_verdict(workspace_agent, tracker=tracker)

    assert verdict is not None
    assert verdict.detail == "the exact error"


@pytest.mark.parametrize(
    "reason",
    [
        # Reached the network and failed there: the workspace is still implicated.
        SystemInterfaceBackendFailureReason.CONNECT_ERROR,
        # The host answered and refused the inner port -- about the workspace,
        # not this device.
        SystemInterfaceBackendFailureReason.BACKEND_NOT_LISTENING,
    ],
)
def test_device_verdict_is_withheld_for_a_failure_that_reached_the_network(
    reason: SystemInterfaceBackendFailureReason,
) -> None:
    """Only a failure raised against this device's own resources clears it.

    Anything that reached the network leaves the workspace's own reachability
    open, and claiming otherwise would suppress the restart that fixes it.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(workspace_agent, reason, "boom")

    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is None


def test_device_verdict_outranks_the_restart_episodes_own_conclusion(tmp_path: Path) -> None:
    """A restart that ran and failed does not displace the verdict that explains it.

    The app restarts a machine that stops answering without being asked, so a
    device-side fault produces RESTARTING and then RESTART_FAILED all by itself.
    Reporting those would blame the machine for the app's own broken connection,
    which is the whole misdiagnosis this verdict exists to stop.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(
        workspace_agent, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no known_hosts"
    )

    tracker.mark_stuck(workspace_agent)
    tracker.mark_restarting(workspace_agent, start_only=True)
    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is not None

    tracker.mark_restart_failed(workspace_agent, "The system interface did not respond.")
    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is not None


def test_device_verdict_clears_the_moment_the_machine_answers() -> None:
    """A probe that reaches the machine settles it: the connection works now."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    tracker.record_connection_failure(
        workspace_agent, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout"
    )

    tracker.record_probe_success(workspace_agent)

    assert read_device_cannot_connect_verdict(workspace_agent, tracker=tracker) is None


def test_device_verdict_is_none_without_a_tracker() -> None:
    """No tracker is no evidence, not a verdict."""
    assert read_device_cannot_connect_verdict(AgentId.generate(), tracker=None) is None


# -- a start that booted nothing --


def test_a_start_that_booted_nothing_is_recorded_against_the_episode(tmp_path: Path) -> None:
    """A no-op start that leaves the machine unreachable must not read as a failed restart.

    The unattended dispatch fires ``mngr start`` at any machine that stops
    answering, and that start is idempotent: against a host that is already up
    it does nothing at all. The tracker still reaches RESTART_FAILED -- the
    machine really did not come back -- but the surfaces read the recorded
    no-op and report the machine as unresponsive instead of blaming a restart
    that never ran.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, was_host_started=False),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            # Port 1 refuses every probe, so the readiness wait runs out: the
            # machine stayed unreachable after a start that started nothing.
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            skip_stop=True,
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert tracker.is_restart_a_no_op(workspace_agent) is True


def test_a_start_that_really_booted_the_host_keeps_the_restart_framing(tmp_path: Path) -> None:
    """A cold boot that did not converge *is* a failed restart, and still reads as one."""
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, AgentId.generate())

    with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs():
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, was_host_started=True),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=_started_registry(workspace_agent),
            skip_stop=True,
            startup_wait_seconds=0.1,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert tracker.is_restart_a_no_op(workspace_agent) is False


# -- post-restart readiness wait --


def test_restart_readiness_budget_covers_a_cold_boot() -> None:
    """The restart's readiness budget is the calibrated cold-boot budget, not its own guess.

    ``mngr start`` cold-boots the container, so the restart worker waits for
    exactly the event the create flow already measures. Sizing both from one
    constant is what keeps an ordinary slow restart from tripping the failure
    branch: the budget was independently set to 30s against a cold boot that
    regularly runs 90-180s, so a workspace that was merely slow got reported --
    at error level, to error reporting -- as a failed restart.
    """
    assert _HOST_RESTART_STARTUP_WAIT_SECONDS == WORKSPACE_READY_TIMEOUT_SECONDS


def test_run_restart_sequence_recovers_a_workspace_that_boots_slowly(tmp_path: Path) -> None:
    """A workspace that only answers after several polls recovers rather than failing.

    The interface stays 503 for the first two polls and answers on the third, so
    the wait must keep polling across them and end in HEALTHY with the operation
    DONE -- and, crucially, log no error: a slow boot is not a failed restart.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with scripted_workspace_probe_server(not_ready_count=2) as port:
        with ConcurrencyGroup(name="test-restart") as cg, capture_error_logs() as error_records:
            run_restart_sequence(
                workspace_agent_id=workspace_agent,
                tracker=tracker,
                backend_resolver=resolver,
                mngr_binary=_write_fake_mngr(tmp_path),
                mngr_host_dir=tmp_path,
                concurrency_group=cg,
                mngr_forward_port=port,
                mngr_forward_preauth_cookie="cookie",
                registry=registry,
                skip_stop=True,
                # Comfortably past the ~2s the scripted boot takes, so the
                # budget is not what ends the wait.
                startup_wait_seconds=30.0,
            )

    assert tracker.get_health(workspace_agent) == AgentHealth.HEALTHY
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.DONE
    assert error_records == []


def test_await_system_interface_ready_reports_a_slow_boot_that_answers() -> None:
    """The wait polls past not-ready responses and reports READY on the first 200."""
    with scripted_workspace_probe_server(not_ready_count=2) as port:
        with ConcurrencyGroup(name="test-wait") as cg:
            outcome = _await_system_interface_ready(
                str(AgentId.generate()), port, "cookie", 30.0, concurrency_group=cg
            )
    assert outcome is RestartReadinessOutcome.READY


def test_await_system_interface_ready_times_out_when_nothing_ever_answers() -> None:
    """A budget that elapses with no answer is a TIMED_OUT verdict (the real failure)."""
    with ConcurrencyGroup(name="test-wait") as cg:
        # Port 1 refuses connections, so every poll fails fast.
        outcome = _await_system_interface_ready(str(AgentId.generate()), 1, "cookie", 0.1, concurrency_group=cg)
    assert outcome is RestartReadinessOutcome.TIMED_OUT


def test_await_system_interface_ready_gives_up_promptly_on_shutdown() -> None:
    """A shutdown cuts the wait short well inside the cold-boot budget, and is not a timeout.

    The budget spans a full cold boot -- far longer than the concurrency group's
    ~10s exit budget -- so a quit during a restart must not leave this thread
    parked until the budget expires. Sleeping on the shutdown event rather than a
    bare timer is what bounds it.
    """
    with ConcurrencyGroup(name="test-wait") as cg:
        cg.shutdown()
        started = time.monotonic()
        # Port 1 refuses connections, so only the shutdown check can end this.
        outcome = _await_system_interface_ready(
            str(AgentId.generate()), 1, "cookie", _HOST_RESTART_STARTUP_WAIT_SECONDS, concurrency_group=cg
        )
        elapsed = time.monotonic() - started

    assert outcome is RestartReadinessOutcome.ABANDONED
    assert elapsed < 5.0, f"the wait ran {elapsed:.1f}s past a shutdown"


def test_run_restart_sequence_does_not_report_a_failure_when_shutdown_cuts_it_short(tmp_path: Path) -> None:
    """A restart truncated by app shutdown is not reported as a failed restart.

    Shutdown says nothing about whether the workspace recovered, and the tracker
    and operation registry are per-process, so there is nothing left to render a
    verdict to. Claiming RESTART_FAILED here would report a failure that was
    never observed -- and would reach error reporting on every quit that lands
    mid-restart.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    tracker.mark_restarting(workspace_agent, start_only=True)
    resolver = build_resolver_with_system_services(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart") as cg:
        # Quit at the first readiness probe: by then ``mngr start`` has already
        # run, so this lands the shutdown inside the wait -- the real ordering --
        # rather than before the sequence's subprocess steps.
        with scripted_workspace_probe_server(not_ready_count=10**6, on_first_request=cg.shutdown) as port:
            with capture_error_logs() as error_records:
                run_restart_sequence(
                    workspace_agent_id=workspace_agent,
                    tracker=tracker,
                    backend_resolver=resolver,
                    mngr_binary=_write_fake_mngr(tmp_path),
                    mngr_host_dir=tmp_path,
                    concurrency_group=cg,
                    mngr_forward_port=port,
                    mngr_forward_preauth_cookie="cookie",
                    registry=registry,
                    skip_stop=True,
                    startup_wait_seconds=_HOST_RESTART_STARTUP_WAIT_SECONDS,
                )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTARTING
    assert error_records == []
    record = registry.get(workspace_agent)
    assert record is not None and record.status == WorkspaceOperationStatus.RUNNING


def _resolver_for_a_remote_machine(workspace_agent: AgentId, services_agent: AgentId) -> MngrCliBackendResolver:
    """The fixture every connectivity-gate test needs: a machine on a backend this device dials.

    Spelled out rather than left to the default builder, which names a provider
    it seeds no snapshot for -- so the machine reaches the gated path through
    ``is_network_dependent_workspace``'s "cannot identify the provider" fallback
    rather than through its backend. That answers the same way today, but it is
    not the condition any of these tests are about, and a fixture that started
    describing its provider would silently invert every one of them.
    """
    return build_resolver_with_system_services(
        workspace_agent,
        services_agent,
        provider_name=ProviderInstanceName("imbue_cloud_someone"),
        provider_backend="imbue_cloud",
    )


def _offline_detector(concurrency_group: ConcurrencyGroup) -> tuple[ConnectivityDetector, StubNetworkProber]:
    """A detector that has just measured a dead network, and the prober to revive it with."""
    detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False, is_ssh_up=False)
    assert detector.probe_now().environment_block is EnvironmentBlock.OFFLINE
    return detector, prober


def _fail_the_walk_behind_the_probe() -> None:
    """Stand-in for everything a probe reaches that is not the socket, failing."""
    raise MindError("the walk behind this probe's endpoints failed")


def test_a_wedge_while_this_device_is_offline_withholds_the_start(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """Every remote machine goes stuck at once when the wifi drops, and every start would fail.

    The machine stays STUCK -- that is true -- and the surfaces read the
    device's condition off the detector's published state, so the card can say
    so instead of narrating a restart that was never dispatched.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, _prober = _offline_detector(root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-offline") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert poll_until(
            lambda: str(workspace_agent) in dispatcher._owed_agent_ids,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "the withheld start must be owed to the machine it was withheld from"

    assert _read_fake_mngr_invocations(mngr_binary) == []
    assert tracker.get_health(workspace_agent) is AgentHealth.STUCK


def test_the_withheld_start_runs_when_connectivity_returns(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, prober = _offline_detector(root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-owed") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        detector.add_on_recovery_callback(dispatcher.on_connectivity_recovered)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert poll_until(
            lambda: str(workspace_agent) in dispatcher._owed_agent_ids,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        )

        bring_stub_network_back(detector, prober)

        assert _wait_for_mngr_invocation(mngr_binary, "start "), "the owed start must run once the network is back"

    assert dispatcher._owed_agent_ids == set(), "a start that ran is no longer owed"


class _TrackerRefusingOneRelease(SystemInterfaceHealthTracker):
    """A tracker whose health read raises for one machine, once armed.

    Poses a release that fails part-way through the drain, which is the only
    thing that can strand the machines behind it: the owed set is emptied before
    any of it runs, and the detector has already stopped watching by then.
    Armed only once the gates are done, because the gate workers read the same
    health on their way into the owed set.
    """

    refused_agent_id: AgentId | None = Field(default=None, description="The machine whose release raises")
    is_refusal_armed: bool = Field(default=False, description="Off while the gates run, on for the drain")

    def get_health(self, agent_id: AgentId) -> AgentHealth:
        if self.is_refusal_armed and agent_id == self.refused_agent_id:
            raise MindError("simulated failure while releasing an owed start")
        return super().get_health(agent_id)


def test_a_release_that_fails_does_not_strand_the_machines_behind_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The drain gets one attempt, so it must not spend it all on the first machine.

    The set is cleared before the first dispatch and the detector stops watching
    at the recovery, so a machine dropped here waits behind a
    waiting-for-network card on a working network with nothing left to come back
    for it.

    The drain is called directly rather than through a probe: a gate worker
    still in flight would run its own late-join drain and dispatch the second
    machine anyway, which is a different guard and would mask this one.
    """
    # The drain walks the set in sorted order, so the machine that raises has to
    # be the lower id for this to pose the question at all.
    lower_id, higher_id = sorted((str(AgentId.generate()), str(AgentId.generate())))
    refused_agent, workspace_agent = AgentId(lower_id), AgentId(higher_id)
    services_agent = AgentId.generate()
    tracker = _TrackerRefusingOneRelease(stuck_threshold_seconds=0.0, refused_agent_id=refused_agent)
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, prober = _offline_detector(root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-owed-partial") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        for agent_id in (refused_agent, workspace_agent):
            tracker.record_failure(agent_id)
            tracker.record_probe_failure(agent_id)
        assert poll_until(
            lambda: dispatcher._owed_agent_ids == {str(refused_agent), str(workspace_agent)},
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "both gate workers must have owed their starts, so the drain sees both of them"
        assert poll_until(
            lambda: not any(t.name.startswith("unattended-recovery-gate-") for t in threading.enumerate()),
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "and must have finished, so no late-join drain of their own can mask this one"

        tracker.is_refusal_armed = True
        bring_stub_network_up(prober)
        dispatcher.on_connectivity_recovered()

        # Any start at all is the second machine's: the first raises before it
        # reaches a dispatch.
        assert _wait_for_mngr_invocation(mngr_binary, "start "), (
            "the second machine's owed start must survive the first one's failure"
        )


def test_an_owed_start_is_dropped_for_a_machine_that_is_no_longer_stuck(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The outage was the network the whole time, so most machines answer again on their own."""
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, prober = _offline_detector(root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-owed-drop") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        detector.add_on_recovery_callback(dispatcher.on_connectivity_recovered)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert poll_until(
            lambda: str(workspace_agent) in dispatcher._owed_agent_ids,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        )

        # The machine answers again the moment the network is back, which the
        # probe loop reports before (or instead of) the owed start running.
        tracker.record_probe_success(workspace_agent)
        # The recovery callback runs inline, so the whole owed set is resolved by
        # the time this returns; a dispatch would leave a worker behind it.
        bring_stub_network_back(detector, prober)
        assert not poll_until(
            lambda: any(line.startswith("start ") for line in _read_fake_mngr_invocations(mngr_binary)),
            timeout=1.0,
            poll_interval=0.02,
        ), "a machine that answered again on its own needs no start"

    assert tracker.get_health(workspace_agent) is AgentHealth.HEALTHY


class _TrackerRecoveringTheNetworkMidGate(SystemInterfaceHealthTracker):
    """Brings the network back inside the gate's post-probe window, once.

    The gate re-reads the machine's health between its probe and the owed-set
    insert, so a recovery fired from that read lands exactly where the drain
    races the insert: the bad -> good edge drains an owed set the machine has
    not joined yet, and no second edge is coming.
    """

    recover_network_fn: Callable[[], None] | None = Field(
        default=None, description="Fired from the first health read after construction"
    )
    _has_recovered: bool = PrivateAttr(default=False)

    def get_health(self, agent_id: AgentId) -> AgentHealth:
        health = super().get_health(agent_id)
        if self.recover_network_fn is not None and not self._has_recovered:
            self._has_recovered = True
            self.recover_network_fn()
        return health


def test_a_recovery_that_lands_while_the_start_is_being_owed_still_runs_it(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The drain fires on the bad -> good edge alone, and there is only ever one of those.

    A machine that joins the owed set just after that edge would otherwise sit
    STUCK forever behind a device condition that no longer holds, with no
    further probe coming to notice.
    """
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, prober = _offline_detector(root_concurrency_group)
    tracker = _TrackerRecoveringTheNetworkMidGate(
        stuck_threshold_seconds=0.0,
        recover_network_fn=lambda: bring_stub_network_back(detector, prober),
    )

    with ConcurrencyGroup(name="test-unattended-owed-race") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        detector.add_on_recovery_callback(dispatcher.on_connectivity_recovered)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)

        assert _wait_for_mngr_invocation(mngr_binary, "start "), "the owed start must not be stranded by the drain"

    assert dispatcher._owed_agent_ids == set(), "the start that ran is no longer owed"


def test_an_unknown_reading_dispatches_exactly_as_before(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A reading a wake disqualified knows nothing, and nothing here may suppress on that.

    The one path that hands the gate an UNKNOWN in production, since its own
    ``probe_now`` measures whenever there is no fresh reading to reuse: the
    laptop wakes while that probe is in flight, and the detector answers with
    the blanked reading instead of a description of the network it went to sleep
    on. The network under the wake is dead, so a measurement that was wrongly
    kept would read OFFLINE and withhold the start -- which is what makes this
    the UNKNOWN case rather than another clear-network one.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    def _wake_the_detector() -> None:
        detector.invalidate_after_wake(datetime.now(timezone.utc))

    prober = SideEffectingStubNetworkProber(
        reachable_hosts=set(), ssh_endpoints=set(), on_first_question=_wake_the_detector
    )
    detector = build_connectivity_detector_over(prober, root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-unknown") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "an unmeasured network must never withhold a restart"
    reading = detector.get_reading()
    assert reading.observed_at is None, "the wake must have left the gate with a reading nobody took"
    assert reading.internet is ConnectivityFacet.UNKNOWN


def test_a_restart_that_fails_while_offline_is_still_restart_failed_but_not_error_logged(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The state is truthful and the user can retry it; the error report would be noise.

    Nobody can act on "the start command failed because this laptop has no
    network", and the report's own log upload would be making the same doomed
    call. Warning keeps it in the local log, out of error reporting.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart-offline") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, start_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
            skip_stop=True,
            connectivity_detector=_offline_detector(root_concurrency_group)[0],
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert error_records == []


def test_a_stop_that_fails_while_offline_is_downgraded_the_same_way_the_start_is(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The stop is as doomed as the start, and as little the machine's fault.

    Both steps report through one helper for exactly this reason. Re-inlining
    the report into the start branch would restore the error-level Sentry
    report for a stop nobody could have completed, which is what this pins.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart-offline-stop") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, stop_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
            connectivity_detector=_offline_detector(root_concurrency_group)[0],
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert error_records == []


def test_a_readiness_wait_that_times_out_while_offline_is_downgraded_too(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The longer of the two windows the network can die in, and the same argument.

    The commands can only fail while offline if the network was already down
    when they ran; the readiness wait is given a full cold-boot budget, so it is
    where a restart that was in flight when the wifi dropped actually ends up --
    and every poll of it routes over the same dead network. Left at error level
    this is the burst of reports the gate exists to prevent, arriving from the
    one machine the gate could not have withheld.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)

    with ConcurrencyGroup(name="test-restart-offline-wait") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            # The start succeeds; it is the wait for the interface that does not.
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            # Port 1 refuses connections, so every poll of the wait fails fast.
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=registry,
            skip_stop=True,
            startup_wait_seconds=0.1,
            connectivity_detector=_offline_detector(root_concurrency_group)[0],
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert error_records == []


def test_a_readiness_wait_that_times_out_on_a_working_network_still_error_logs(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A machine that never answered on a network that is fine is the machine's own failure."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group)
    assert detector.probe_now().environment_block is EnvironmentBlock.NONE

    with ConcurrencyGroup(name="test-restart-online-wait") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=1,
            mngr_forward_preauth_cookie="cookie",
            registry=registry,
            skip_stop=True,
            startup_wait_seconds=0.1,
            connectivity_detector=detector,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert len(error_records) == 1


def test_a_restart_that_fails_on_a_working_network_still_error_logs(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The downgrade is scoped to the one cause that explains the failure away."""
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    registry = _started_registry(workspace_agent)
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group)
    assert detector.probe_now().environment_block is EnvironmentBlock.NONE

    with ConcurrencyGroup(name="test-restart-online") as cg, capture_error_logs() as error_records:
        run_restart_sequence(
            workspace_agent_id=workspace_agent,
            tracker=tracker,
            backend_resolver=resolver,
            mngr_binary=_write_fake_mngr(tmp_path, start_exit=1),
            mngr_host_dir=tmp_path,
            concurrency_group=cg,
            mngr_forward_port=0,
            mngr_forward_preauth_cookie=None,
            registry=registry,
            skip_stop=True,
            connectivity_detector=detector,
        )

    assert tracker.get_health(workspace_agent) == AgentHealth.RESTART_FAILED
    assert len(error_records) == 1


def _ssh_info(host: str, port: int) -> RemoteSSHInfo:
    return RemoteSSHInfo(user="root", host=host, port=port, key_path=Path("/dev/null"))


@pytest.mark.parametrize("backend", ["local", "docker", "lima"])
def test_workspaces_on_this_device_are_exempt_from_connectivity(backend: str) -> None:
    """They are reachable over loopback with the wifi off, so a reading says nothing about them."""
    agent_id = AgentId.generate()
    resolver = build_resolver_with_provider_backend(agent_id, provider_name=backend, backend=backend)

    assert not is_network_dependent_workspace(resolver, agent_id)


def test_workspaces_on_a_remote_backend_are_network_dependent() -> None:
    agent_id = AgentId.generate()
    resolver = build_resolver_with_provider_backend(
        agent_id, provider_name="imbue_cloud_someone", backend="imbue_cloud"
    )

    assert is_network_dependent_workspace(resolver, agent_id)


def test_an_unidentifiable_backend_counts_as_network_dependent() -> None:
    """The conservative direction: at worst a probe confirms the network is fine and the start runs."""
    resolver = MngrCliBackendResolver()

    assert is_network_dependent_workspace(resolver, AgentId.generate())


def test_the_endpoint_sample_leaves_out_machines_that_answer_without_a_network() -> None:
    """A docker container is reached at 127.0.0.1, and loopback answers with the wifi off.

    Discovery reports SSH info for on-device machines like any other host, so
    without the filter one of them would settle the SSH facet as reachable on
    every probe -- and the incompatible-network verdict, which rests entirely on
    that facet, could never fire for anyone running a local machine.
    """
    remote_agent = AgentId.generate()
    docker_agent = AgentId.generate()
    resolver = build_resolver_with_provider_backends(
        (
            SeededAgent(
                agent_id=remote_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=_ssh_info("box.example", 22131),
            ),
            SeededAgent(
                agent_id=docker_agent,
                provider_name="docker",
                backend="docker",
                ssh_info=_ssh_info("127.0.0.1", 2222),
            ),
        )
    )

    endpoints = WorkspaceSshEndpointSource(backend_resolver=resolver)()

    assert endpoints == (SshEndpoint(host="box.example", port=22131),)


def test_the_endpoint_sample_reports_each_host_once_and_skips_machines_without_one() -> None:
    """The agents on one host share its endpoint, and a host discovery has no SSH info for is not one."""
    first_agent = AgentId.generate()
    second_agent = AgentId.generate()
    endpointless_agent = AgentId.generate()
    shared = _ssh_info("box.example", 22131)
    # One host carrying both, which is what a real machine is: the workspace
    # agent and its system-services agent run in the same container and so
    # report the same coordinate.
    shared_host = HostId.generate()
    resolver = build_resolver_with_provider_backends(
        (
            SeededAgent(
                agent_id=first_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=shared,
                host_id=shared_host,
            ),
            SeededAgent(
                agent_id=second_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=shared,
                host_id=shared_host,
            ),
            SeededAgent(agent_id=endpointless_agent, provider_name="imbue_cloud_someone", backend="imbue_cloud"),
        )
    )

    endpoints = WorkspaceSshEndpointSource(backend_resolver=resolver)()

    assert endpoints == (SshEndpoint(host="box.example", port=22131),)


@pytest.mark.parametrize("backend", ["local", "docker", "lima"])
def test_a_machine_on_this_device_is_started_however_dead_the_network_is(
    tmp_path: Path, backend: str, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The gating half of the exemption, which is the half with teeth.

    A docker container answers over loopback with the wifi off, so its restart
    is not routed over anything the network has a say in -- and withholding it
    would leave a wedged machine wedged for as long as the user stays offline,
    with a card offering nothing. It is why ``_ON_DEVICE_PROVIDER_BACKENDS``
    includes ``local``, which the same-shaped set in ``mind_liveness`` leaves
    out. The network is not merely overruled here but never measured: the
    exemption is checked before the gate's worker is ever spawned, so a probe
    taken at all would mean the check had moved.
    """
    tracker = SystemInterfaceHealthTracker()
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(workspace_agent, services_agent, provider_backend=backend)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    # Nothing answers anywhere, so any reading this detector took would be OFFLINE.
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)

    with ConcurrencyGroup(name=f"test-unattended-on-device-{backend}") as cg:
        dispatch = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        dispatch(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "an on-device machine's start is never withheld for the network"
    assert prober.probed_endpoints == [], "the exemption skips the probe rather than surviving it"


def test_a_gate_that_could_not_be_spawned_leaves_the_machine_stuck(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The gate is the one of the branch's three spawns that recovers nothing.

    The trigger leaves its episode unmeasured and the view refresher publishes
    without a settle, but a gate that never starts neither dispatches nor owes:
    the machine stays STUCK with no device explanation and no start. That is the
    right answer -- the group only refuses once the app is going down, so there
    is nothing left to dispatch onto -- but it is a choice, and the alternative
    (dispatch blind rather than not at all) is the behaviour this branch replaced.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    detector, prober = _offline_detector(root_concurrency_group)
    with ConcurrencyGroup(name="test-unattended-gate-exited") as exited_group:
        pass
    probes_before = list(prober.probed_endpoints)
    dispatcher = _dispatcher(tracker, resolver, registry, exited_group, mngr_binary, tmp_path, detector)
    _drive_to_stuck_with_onset(tracker, workspace_agent)

    dispatcher(workspace_agent)

    assert prober.probed_endpoints == probes_before, "the group is gone, so no gate can have measured anything"
    assert _read_fake_mngr_invocations(mngr_binary) == [], "and nothing may be dispatched without the reading"
    assert tracker.get_health(workspace_agent) is AgentHealth.STUCK, "the machine is left exactly as it was"
    assert dispatcher._owed_agent_ids == set(), "a start no gate ran for is not owed either"


def test_a_gate_whose_probe_lost_the_group_drops_the_start(tmp_path: Path) -> None:
    """The group refusing the SSH round is the spawn failure one frame later.

    The gate spawns fine here; what fails is the round inside the probe, which
    the group refuses only once the app is going down -- so there is nothing
    left to dispatch onto, and the machine is left exactly as the un-spawnable
    gate leaves it. The sibling below is the other family the same call is
    fenced for, and it is answered the opposite way; collapsing the two fences
    into one would dispatch a restart that then loses its own spawn and reports
    RESTART_FAILED, at error level, on the way out.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    with ConcurrencyGroup(name="test-unattended-probe-group-exited") as exited_group:
        pass
    # A working internet, so the probe reaches the SSH round at all: with no
    # workspace endpoints the first round answers without an executor, and the
    # public quorum's is the one the exited group refuses to build.
    prober = StubNetworkProber(reachable_hosts=set(STUB_CONNECTIVITY_HOSTS))
    detector = build_connectivity_detector_over(prober, exited_group)
    _drive_to_stuck_with_onset(tracker, workspace_agent)

    with ConcurrencyGroup(name="test-unattended-gate-losing-the-round") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        dispatcher(workspace_agent)
        assert poll_until(lambda: prober.probed_endpoints != [], timeout=_DISPATCH_WAIT_SECONDS, poll_interval=0.02), (
            "the gate must have reached the probe"
        )

    assert _read_fake_mngr_invocations(mngr_binary) == [], "a reading the group refused dispatches nothing"
    assert tracker.get_health(workspace_agent) is AgentHealth.STUCK, "the machine is left exactly as it was"
    assert dispatcher._owed_agent_ids == set(), "a start dropped this way is not owed either"


def test_a_gate_whose_probe_raised_still_starts_the_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A reading that could not be taken is no evidence, and no evidence withholds nothing.

    The other half of the spawn failure above, and the one that cannot be
    answered the same way. That one drops because the group refusing the thread
    means the app is going down; this one is the walk behind the endpoints
    failing on an app that is perfectly healthy -- the hazard
    ``run_background_loop`` names and the sibling trigger has its own test for.
    Dropped here too, the machine would be neither started, nor marked, nor
    owed, and nothing would ever come back for it: the owed set is drained by a
    connectivity recovery, and a probe that never landed produces no edge. That
    is worse than the unconditional dispatch this gate replaced.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    prober = SideEffectingStubNetworkProber(on_first_question=_fail_the_walk_behind_the_probe)
    detector = build_connectivity_detector_over(prober, root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-gate-raising-probe") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        is_started = _wait_for_mngr_invocation(mngr_binary, "start ")

    assert is_started, "a machine whose gate could not measure the network is started, not stranded"
    assert dispatcher._owed_agent_ids == set(), "nor is it owed a start it has already been given"


def test_a_machine_stopped_while_the_gate_probes_is_not_started_anyway(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A stop lands mid-probe, and the mark it set before its command must still be honoured.

    The in-app stop and the destroy route both mark the machine before their own
    command runs, precisely so this dispatch cannot undo them -- and a destroy
    takes no operation slot, so the dispatch's own claim would not catch it
    either. The gate reads the mark before a probe that costs seconds, so it has
    to read it again after.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()
    # A perfectly healthy network, so nothing but the stop can withhold the start.
    prober = SideEffectingStubNetworkProber(
        reachable_hosts=set(STUB_CONNECTIVITY_HOSTS),
        ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS),
        on_first_question=lambda: tracker.suppress_unattended_recovery(workspace_agent, is_stop_in_flight=True),
    )
    detector = build_connectivity_detector_over(prober, root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-stopped-mid-probe") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert poll_until(lambda: prober.probed_endpoints != [], timeout=_DISPATCH_WAIT_SECONDS, poll_interval=0.02), (
            "the gate must have measured the network"
        )

    assert _read_fake_mngr_invocations(mngr_binary) == [], "a machine stopped from inside the app is not started"


def test_a_restart_that_wins_the_machine_mid_probe_is_not_overwritten(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A restart the user asked for owns the machine, and the gate must not owe it another start.

    An owed start released at the network's return would land on top of the
    restart that is already running -- and the withheld-start log would say a
    start was withheld from a machine that is in fact being restarted.
    """
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    resolver = _resolver_for_a_remote_machine(workspace_agent, services_agent)
    mngr_binary = _write_fake_mngr(tmp_path)
    registry = InMemoryWorkspaceOperationRegistry()

    # A dead network, so the gate has a condition to record.
    def _start_a_restart_mid_probe() -> None:
        tracker.mark_restarting(workspace_agent, start_only=True)

    prober = SideEffectingStubNetworkProber(on_first_question=_start_a_restart_mid_probe)
    detector = build_connectivity_detector_over(prober, root_concurrency_group)

    with ConcurrencyGroup(name="test-unattended-restarting-mid-probe") as cg:
        dispatcher = _dispatcher(tracker, resolver, registry, cg, mngr_binary, tmp_path, detector)
        tracker.add_on_stuck_edge_callback(dispatcher)
        tracker.record_failure(workspace_agent)
        tracker.record_probe_failure(workspace_agent)
        assert poll_until(lambda: prober.probed_endpoints != [], timeout=_DISPATCH_WAIT_SECONDS, poll_interval=0.02), (
            "the gate must have measured the network"
        )

    assert tracker.get_health(workspace_agent) is AgentHealth.RESTARTING
    # The gate's own share of the race: without its re-read of the health after
    # the probe, the machine joins the owed set and logs a withheld start over a
    # restart that is genuinely running.
    assert dispatcher._owed_agent_ids == set(), "a machine a restart already claimed is not owed one"


def _record_errored_provider(resolver: MngrCliBackendResolver, provider_name: str, backend: str) -> None:
    """Report ``provider_name`` as known but unreachable, as a failed poll does."""
    resolver.update_providers(
        ProviderInstanceName(provider_name),
        provider=DiscoveredProvider(
            provider_name=ProviderInstanceName(provider_name),
            config=PersistedProviderInstanceConfig(backend=ProviderBackendName(backend)),
        ),
        error=DiscoveryError(
            type_name="ProviderUnavailableError",
            message="could not reach the provider",
            provider_name=ProviderInstanceName(provider_name),
        ),
        last_snapshot_at=datetime.now(timezone.utc),
    )


def _resolver_with_errored_provider(provider_name: str, backend: str) -> MngrCliBackendResolver:
    """A resolver whose only provider failed its poll, as a dead network produces."""
    resolver = MngrCliBackendResolver()
    _record_errored_provider(resolver, provider_name=provider_name, backend=backend)
    return resolver


def test_an_unreachable_remote_provider_measures_the_network_with_nothing_convicted(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The cold-start case: minds opened on a dead network, before anything is clicked.

    Nothing has been asked to load, so no machine is a probe suspect and none can
    go STUCK -- the gate that would otherwise ask about the network never runs.
    Discovery's own failed poll is the earliest evidence there is.
    """
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)

    with ConcurrencyGroup(name="test-provider-error-trigger") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()
        assert poll_until(
            lambda: detector.get_reading().environment_block is EnvironmentBlock.OFFLINE,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "a provider discovery cannot reach must be enough to measure the network"

    assert prober.probed_endpoints != []


def test_an_unreachable_on_device_provider_measures_nothing(root_concurrency_group: ConcurrencyGroup) -> None:
    """A stopped docker daemon errors the same way and says nothing about the network."""
    resolver = _resolver_with_errored_provider("docker", backend="docker")
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)

    with ConcurrencyGroup(name="test-provider-error-trigger-local") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()

    assert prober.probed_endpoints == []
    assert detector.get_reading().environment_block is EnvironmentBlock.NONE


def test_a_healthy_provider_measures_nothing(root_concurrency_group: ConcurrencyGroup) -> None:
    """Steady state stays silent: nothing is probed while discovery is fine."""
    agent_id = AgentId.generate()
    resolver = build_resolver_with_provider_backend(
        agent_id, provider_name="imbue_cloud_someone", backend="imbue_cloud"
    )
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)

    with ConcurrencyGroup(name="test-provider-error-trigger-healthy") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()

    assert prober.probed_endpoints == []


def test_the_resolver_s_chatter_does_not_stack_probes(root_concurrency_group: ConcurrencyGroup) -> None:
    """The resolver fires on every discovery event; the network is not measured per event."""
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)

    with ConcurrencyGroup(name="test-provider-error-trigger-chatter") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        for _ in range(20):
            trigger()
        assert poll_until(
            lambda: detector.get_reading().environment_block is EnvironmentBlock.OFFLINE,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        )

    # Exactly one reading's worth of endpoints, not twenty: the in-flight latch
    # is set before the worker is spawned, so no second worker exists to race the
    # first. Equality rather than a bound, so a trigger that stopped probing
    # altogether fails here too.
    assert len(prober.probed_endpoints) == len(STUB_CONNECTIVITY_HOSTS)


def test_a_provider_error_that_never_clears_is_measured_once_not_forever(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A broken provider on a working network must not turn into a network poll.

    The trigger fires on every discovery event, and a provider can stay errored
    for the life of the app -- a revoked token, a backend outage of its own.
    Latched on the in-flight flag and the reading-reuse window alone, that is a
    probe of three public hosts every couple of seconds forever, which is the
    background polling the detector exists without.
    """
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    clock = ManualClock(datetime.now(timezone.utc))
    prober = StubNetworkProber(reachable_hosts=set(STUB_CONNECTIVITY_HOSTS), ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS))
    detector = build_connectivity_detector_over(prober, root_concurrency_group, now_fn=clock)

    with ConcurrencyGroup(name="test-provider-error-trigger-persistent") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()
        assert poll_until(
            lambda: detector.get_reading().observed_at is not None and not trigger._is_probe_in_flight,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "the first provider error must be measured"
        probes_after_the_first = len(prober.probed_endpoints)

        # Long past any reading-reuse window, with the same provider still the
        # only one erroring: no new evidence, so nothing new to measure.
        clock.advance(3600.0)
        for _ in range(5):
            trigger()

    assert len(prober.probed_endpoints) == probes_after_the_first


def test_a_second_provider_going_dark_is_new_evidence_and_is_measured(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A network dying under an already-broken provider takes the others with it."""
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    clock = ManualClock(datetime.now(timezone.utc))
    prober = StubNetworkProber(reachable_hosts=set(STUB_CONNECTIVITY_HOSTS), ssh_endpoints=set(PUBLIC_SSH_ENDPOINTS))
    detector = build_connectivity_detector_over(prober, root_concurrency_group, now_fn=clock)

    with ConcurrencyGroup(name="test-provider-error-trigger-second-provider") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()
        assert poll_until(
            lambda: detector.get_reading().observed_at is not None and not trigger._is_probe_in_flight,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        )
        probes_after_the_first = len(prober.probed_endpoints)

        clock.advance(3600.0)
        prober.reachable_hosts = set()
        _record_errored_provider(resolver, provider_name="aws_someone", backend="aws")
        trigger()
        assert poll_until(
            lambda: detector.get_reading().environment_block is EnvironmentBlock.OFFLINE,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "a provider that was reachable a moment ago and is not now must be measured"

    assert len(prober.probed_endpoints) > probes_after_the_first


def test_an_episode_whose_probe_never_started_is_left_unmeasured(root_concurrency_group: ConcurrencyGroup) -> None:
    """A worker that could not be spawned must not latch the errors it was going to ask about.

    Both the in-flight flag and the episode are set *before* the spawn, so a
    group that refuses the thread would otherwise leave the trigger comparing
    every later event against an errored set no probe ever asked about, and
    holding a flag no worker will ever clear -- silent for the life of the app,
    on exactly the cold start over a dead network it exists for. The latch is
    the state itself, so that is what this reads.
    """
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)
    with ConcurrencyGroup(name="test-provider-error-trigger-exited") as exited_group:
        pass
    trigger = ProviderErrorConnectivityTrigger(
        backend_resolver=resolver, connectivity_detector=detector, concurrency_group=exited_group
    )

    trigger()

    assert prober.probed_endpoints == [], "the group is gone, so no probe can have run"
    assert trigger._measured_provider_errors is None, "an episode no probe ran for is not a measured one"
    assert not trigger._is_probe_in_flight, "and no worker is coming to clear the flag"


def test_an_episode_whose_probe_raised_is_left_unmeasured(root_concurrency_group: ConcurrencyGroup) -> None:
    """A probe that raised part-way asked nothing, so the episode is not one that has been asked.

    The other half of the spawn failure above, and the one the flag alone does
    not cover: the worker started, so ``_is_probe_in_flight`` clears either way,
    but the episode was recorded as measured *before* the probe ran. Latched
    there, every later event of the same episode compares equal and returns --
    leaving the hub pages silent for the rest of a cold start over a dead
    network, which is the one thing this trigger exists to speak for.
    """
    resolver = _resolver_with_errored_provider("imbue_cloud_someone", backend="imbue_cloud")
    prober = SideEffectingStubNetworkProber(on_first_question=_fail_the_walk_behind_the_probe)
    detector = build_connectivity_detector_over(prober, root_concurrency_group)

    with ConcurrencyGroup(name="test-provider-error-trigger-raising-probe") as cg:
        trigger = ProviderErrorConnectivityTrigger(
            backend_resolver=resolver, connectivity_detector=detector, concurrency_group=cg
        )
        trigger()
        assert poll_until(
            lambda: not prober.is_armed and not trigger._is_probe_in_flight,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "the worker must have reached the probe that raises, and finished"
        assert trigger._measured_provider_errors is None, "an episode whose probe raised is not a measured one"

        # And is measured by the next event of the same episode, which is what
        # un-latching it buys.
        trigger()
        assert poll_until(
            lambda: detector.get_reading().observed_at is not None,
            timeout=_DISPATCH_WAIT_SECONDS,
            poll_interval=0.02,
        ), "the same episode must still be measurable"


def test_the_endpoint_sample_asks_running_hosts_first() -> None:
    """A stopped host cannot answer whatever the network is doing.

    The detector samples only the first few endpoints and one answering settles
    the facet, so spending the sample on hosts that cannot answer is what sends
    it to the public quorum -- where a network blocking port 22 in particular
    then reads as blocking SSH outright, and withholds a restart the machine may
    genuinely need.
    """
    stopped_agent = AgentId.generate()
    running_agent = AgentId.generate()
    resolver = build_resolver_with_provider_backends(
        (
            SeededAgent(
                agent_id=stopped_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=_ssh_info("stopped.example", 22131),
                host_state=HostState.STOPPED,
            ),
            SeededAgent(
                agent_id=running_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=_ssh_info("running.example", 22132),
                host_state=HostState.RUNNING,
            ),
        )
    )

    endpoints = WorkspaceSshEndpointSource(backend_resolver=resolver)()

    # The stopped host's endpoint follows rather than being dropped: ordering,
    # not filtering, is what keeps a reading possible when discovery is stale.
    assert endpoints == (
        SshEndpoint(host="running.example", port=22132),
        SshEndpoint(host="stopped.example", port=22131),
    )


def test_a_host_discovery_cannot_describe_is_still_asked_about() -> None:
    """Ordering, not filtering: a dead network leaves discovery stale too.

    A reading taken when nothing is *known* to be running still has to be able
    to ask about something, or the facet would fall through to the public quorum
    exactly when minds' own endpoints are the more informative answer.
    """
    unknown_agent = AgentId.generate()
    resolver = build_resolver_with_provider_backends(
        (
            SeededAgent(
                agent_id=unknown_agent,
                provider_name="imbue_cloud_someone",
                backend="imbue_cloud",
                ssh_info=_ssh_info("unknown.example", 22131),
            ),
        )
    )

    endpoints = WorkspaceSshEndpointSource(backend_resolver=resolver)()

    assert endpoints == (SshEndpoint(host="unknown.example", port=22131),)
