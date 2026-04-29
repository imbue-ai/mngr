import os
import signal
import socket
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import psutil
import pytest
from pydantic import PrivateAttr

from imbue.minds.desktop_client.latchkey.gateway import AGENT_SIDE_LATCHKEY_PORT
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyBinaryNotFoundError
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayDestructionHandler
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayDiscoveryHandler
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayManager
from imbue.minds.desktop_client.latchkey.gateway import LatchkeyGatewayManagerNotStartedError
from imbue.minds.desktop_client.latchkey.gateway import _cmdline_looks_like_latchkey_gateway
from imbue.minds.desktop_client.latchkey.store import LatchkeyGatewayInfo
from imbue.minds.desktop_client.latchkey.store import ensure_browser_log_path
from imbue.minds.desktop_client.latchkey.store import load_gateway_info
from imbue.minds.desktop_client.latchkey.store import save_gateway_info
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.mngr.primitives import AgentId

_POLL_INTERVAL_SECONDS = 0.05


def test_cmdline_matcher_accepts_plausible_latchkey_gateway() -> None:
    assert _cmdline_looks_like_latchkey_gateway(["/usr/local/bin/latchkey", "gateway"])
    assert _cmdline_looks_like_latchkey_gateway(["latchkey", "gateway", "--verbose"])
    # Shebang rewriting: kernel injects the interpreter ahead of the script path.
    assert _cmdline_looks_like_latchkey_gateway(["/usr/bin/env", "node", "/opt/latchkey/cli", "gateway"])
    assert _cmdline_looks_like_latchkey_gateway(["node", "gateway"]) is False
    assert _cmdline_looks_like_latchkey_gateway(["latchkey", "auth", "set"]) is False
    assert _cmdline_looks_like_latchkey_gateway([]) is False


def test_ensure_gateway_started_requires_start(tmp_path: Path) -> None:
    manager = LatchkeyGatewayManager()
    with pytest.raises(LatchkeyGatewayManagerNotStartedError):
        manager.ensure_gateway_started(AgentId())


def test_stop_is_idempotent_before_start() -> None:
    manager = LatchkeyGatewayManager()
    manager.stop()


def test_ensure_gateway_started_raises_when_binary_missing(tmp_path: Path) -> None:
    manager = LatchkeyGatewayManager(latchkey_binary=str(tmp_path / "definitely-does-not-exist"))
    manager.start(data_dir=tmp_path)
    try:
        with pytest.raises(LatchkeyBinaryNotFoundError):
            manager.ensure_gateway_started(AgentId())
    finally:
        manager.stop()


def _make_fake_latchkey_binary(tmp_path: Path) -> Path:
    """Build a shell script that imitates ``latchkey gateway``.

    Binds a TCP socket on the host:port supplied via environment variables
    (matching the real binary's contract) and sleeps until terminated.
    Also accepts ``ensure-browser`` as an immediate no-op exit, since the
    gateway manager fires that alongside each gateway spawn.
    """
    script = tmp_path / "latchkey"
    # signal.pause() blocks indefinitely until a signal arrives, letting the
    # script keep the port bound without busy-looping. SIGTERM triggers the
    # handler and exits cleanly. The binary is named "latchkey" (matching
    # the cmdline tag the manager checks against) and accepts "gateway" as
    # argv[1] so the full command looks like ``latchkey gateway``.
    # Listen backlog is large so repeated probe connects from the test don't
    # fill it up (we never explicitly ``accept`` here -- the kernel ACKs the
    # TCP handshake for queued connections, which is all the liveness probe
    # needs). SIGTERM triggers a clean exit; signal.pause blocks indefinitely.
    #
    # The ``ensure-browser`` short-circuit matters for leak detection: the
    # manager fires ``latchkey ensure-browser`` detached on first gateway
    # spawn and intentionally does not reap it. If that subprocess is still
    # in its Python startup when the session-level leak check scans under
    # CI load, it gets flagged as a leak and attributed to some unrelated
    # test. Exiting before any import keeps the process window tiny.
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        "import os, socket, signal\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def _wait_for_listening(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll until something accepts TCP connections on host:port."""
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> bool:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        process.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        return False


def test_ensure_gateway_started_spawns_subprocess_persists_record_and_allocates_port(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    try:
        agent_id = AgentId()
        info = manager.ensure_gateway_started(agent_id)
        assert info.agent_id == agent_id
        assert info.host == "127.0.0.1"
        assert info.port > 0
        assert info.pid > 0
        assert _wait_for_listening(info.host, info.port), "gateway did not start listening"

        # The record was persisted and matches the returned info.
        record = load_gateway_info(tmp_path, agent_id)
        assert record is not None
        assert record.host == info.host
        assert record.port == info.port
        assert record.pid == info.pid

        # Idempotent: a second call returns the same info without spawning again.
        second = manager.ensure_gateway_started(agent_id)
        assert second == info
        assert len(manager.list_gateways()) == 1
    finally:
        manager.stop_gateway_for_agent(agent_id)
        manager.stop()


def test_manager_stop_does_not_kill_running_gateway(tmp_path: Path) -> None:
    """After ``stop()`` the gateway process must still be running -- it must survive
    the desktop client exiting so that in-flight container/VM agents keep working.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    try:
        agent_id = AgentId()
        info = manager.ensure_gateway_started(agent_id)
        assert _wait_for_listening(info.host, info.port)

        manager.stop()

        # In-memory tracking is gone, but the record and process live on.
        assert manager.list_gateways() == ()
        assert load_gateway_info(tmp_path, agent_id) is not None
        assert psutil.pid_exists(info.pid)
        assert _wait_for_listening(info.host, info.port)
    finally:
        # Explicit teardown: we intentionally leaked the process above to prove
        # the survive-parent-exit behaviour, so clean it up directly.
        try:
            os.kill(info.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_restart_adopts_live_gateway_and_discards_stale_info(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    # First "session": start a gateway for a live agent, then stop the manager.
    manager_a = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager_a.start(data_dir=tmp_path)
    live_agent = AgentId()
    info = manager_a.ensure_gateway_started(live_agent)
    assert _wait_for_listening(info.host, info.port)
    manager_a.stop()

    # Inject a stale record (pid points at an unused PID range to simulate a
    # gateway that exited between sessions).
    stale_agent = AgentId()
    stale_info = LatchkeyGatewayInfo(
        agent_id=stale_agent,
        host="127.0.0.1",
        port=1,
        pid=2**31 - 1,
        started_at=datetime.now(timezone.utc),
    )
    save_gateway_info(tmp_path, stale_info)

    try:
        # Second "session": manager should adopt the live record and discard the stale one.
        manager_b = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
        manager_b.start(data_dir=tmp_path)
        try:
            adopted = manager_b.get_gateway_info(live_agent)
            assert adopted is not None
            assert adopted.pid == info.pid
            assert adopted.port == info.port

            assert manager_b.get_gateway_info(stale_agent) is None
            assert load_gateway_info(tmp_path, stale_agent) is None

            # A second ensure_gateway_started for the live agent should reuse
            # the adopted process -- no new PID allocated.
            ensured = manager_b.ensure_gateway_started(live_agent)
            assert ensured.pid == info.pid
        finally:
            manager_b.stop_gateway_for_agent(live_agent)
            manager_b.stop()
    finally:
        if psutil.pid_exists(info.pid):
            try:
                os.kill(info.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_reconcile_with_known_agents_terminates_orphans(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    try:
        live_agent = AgentId()
        orphan_agent = AgentId()
        live_info = manager.ensure_gateway_started(live_agent)
        orphan_info = manager.ensure_gateway_started(orphan_agent)
        assert _wait_for_listening(orphan_info.host, orphan_info.port)

        manager.reconcile_with_known_agents(frozenset({live_agent}))

        # Orphan got terminated, record removed.
        assert manager.get_gateway_info(orphan_agent) is None
        assert load_gateway_info(tmp_path, orphan_agent) is None
        assert _wait_for_process_exit(orphan_info.pid), "orphan process did not exit"

        # Live agent untouched.
        assert manager.get_gateway_info(live_agent) == live_info
        assert load_gateway_info(tmp_path, live_agent) is not None
        assert psutil.pid_exists(live_info.pid)
    finally:
        manager.stop_gateway_for_agent(live_agent)
        manager.stop()


def test_stop_gateway_for_agent_terminates_subprocess_and_removes_record(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    try:
        agent_id = AgentId()
        info = manager.ensure_gateway_started(agent_id)
        assert _wait_for_listening(info.host, info.port)

        manager.stop_gateway_for_agent(agent_id)
        assert manager.get_gateway_info(agent_id) is None
        assert load_gateway_info(tmp_path, agent_id) is None
        assert _wait_for_process_exit(info.pid)
    finally:
        manager.stop()


def test_stop_gateway_for_agent_is_no_op_when_not_running(tmp_path: Path) -> None:
    manager = LatchkeyGatewayManager()
    manager.start(data_dir=tmp_path)
    try:
        manager.stop_gateway_for_agent(AgentId())
    finally:
        manager.stop()


def test_discovery_handler_spawns_for_every_provider(tmp_path: Path) -> None:
    """Every provider -- including cloud/VPS -- gets a gateway now that agents
    on remote hosts reach the desktop via a reverse SSH tunnel."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    tunnel_manager = SSHTunnelManager()
    try:
        handler = LatchkeyGatewayDiscoveryHandler(gateway_manager=manager, tunnel_manager=tunnel_manager)
        agent_by_provider = {name: AgentId() for name in ("local", "docker", "lima", "vultr", "modal")}
        for provider_name, agent_id in agent_by_provider.items():
            # ssh_info=None is fine here -- it keeps the test off the SSH path.
            # The "does it also set up a reverse tunnel when ssh_info is given"
            # behavior is covered by a dedicated test below.
            handler(agent_id, None, provider_name)
        assert {info.agent_id for info in manager.list_gateways()} == set(agent_by_provider.values())
    finally:
        for agent_id in agent_by_provider.values():
            manager.stop_gateway_for_agent(agent_id)
        manager.stop()
        tunnel_manager.cleanup()


class _RecordingTunnelManager(SSHTunnelManager):
    """SSHTunnelManager that records setup_reverse_tunnel calls instead of doing SSH."""

    _calls: list[tuple[RemoteSSHInfo, int, int]] = PrivateAttr(default_factory=list)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        agent_state_dir: str | None = None,
        remote_port: int = 0,
    ) -> int:
        del agent_state_dir
        self._calls.append((ssh_info, local_port, remote_port))
        return remote_port


def test_discovery_handler_sets_up_reverse_tunnel_when_ssh_info_given(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    tunnel_manager = _RecordingTunnelManager()
    try:
        handler = LatchkeyGatewayDiscoveryHandler(gateway_manager=manager, tunnel_manager=tunnel_manager)
        ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=22, key_path=tmp_path / "k")
        agent_id = AgentId()
        handler(agent_id, ssh_info, "docker")

        info = manager.get_gateway_info(agent_id)
        assert info is not None

        # Exactly one reverse tunnel, bridging the dynamic host-side gateway port
        # to the fixed agent-side port on the container's loopback.
        assert tunnel_manager._calls == [(ssh_info, info.port, AGENT_SIDE_LATCHKEY_PORT)]
    finally:
        manager.stop_gateway_for_agent(agent_id)
        manager.stop()


# Flaky on offload: session-level teardown occasionally finds the ``ensure-browser``
# child process still alive, even though the fake binary calls ``sys.exit(0)``
# immediately. Race between subprocess exit and pytest's leak scan; needs a
# proper fix in ``LatchkeyGatewayManager.stop()`` to track + reap that spawn.
@pytest.mark.flaky
def test_discovery_handler_skips_reverse_tunnel_for_dev_agents(tmp_path: Path) -> None:
    """DEV agents (ssh_info is None) run on the bare host and need no tunnel."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    tunnel_manager = _RecordingTunnelManager()
    try:
        handler = LatchkeyGatewayDiscoveryHandler(gateway_manager=manager, tunnel_manager=tunnel_manager)
        agent_id = AgentId()
        handler(agent_id, None, "local")

        assert manager.get_gateway_info(agent_id) is not None
        assert tunnel_manager._calls == []
    finally:
        manager.stop_gateway_for_agent(agent_id)
        manager.stop()


def test_discovery_handler_swallows_gateway_errors(tmp_path: Path) -> None:
    """A missing binary must not crash the discovery callback -- just log a warning."""
    manager = LatchkeyGatewayManager(latchkey_binary=str(tmp_path / "missing"))
    manager.start(data_dir=tmp_path)
    tunnel_manager = _RecordingTunnelManager()
    try:
        handler = LatchkeyGatewayDiscoveryHandler(gateway_manager=manager, tunnel_manager=tunnel_manager)
        handler(AgentId(), None, "local")
        assert manager.list_gateways() == ()
        assert tunnel_manager._calls == []
    finally:
        manager.stop()


def _make_fake_latchkey_binary_with_ensure_browser_counter(tmp_path: Path, counter_path: Path) -> Path:
    """Build a fake ``latchkey`` that handles both ``gateway`` (blocking, like the
    real gateway) and ``ensure-browser`` (increments ``counter_path`` and exits).

    Lets us verify that the manager calls ``ensure-browser`` exactly once per
    session regardless of how many gateways get spawned.
    """
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    counter_path = os.environ['FAKE_LATCHKEY_COUNTER']\n"
        "    open(counter_path, 'a').write('1\\n')\n"
        "    sys.exit(0)\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def _wait_for_counter(counter_path: Path, expected: int, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        if counter_path.is_file():
            last = len(counter_path.read_text().splitlines())
            if last >= expected:
                return last
        threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return last


def test_ensure_browser_runs_once_on_first_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    counter_path = tmp_path / "ensure_browser_counter"
    monkeypatch.setenv("FAKE_LATCHKEY_COUNTER", str(counter_path))
    fake_binary = _make_fake_latchkey_binary_with_ensure_browser_counter(tmp_path, counter_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    agent_ids = [AgentId() for _ in range(3)]
    try:
        for agent_id in agent_ids:
            manager.ensure_gateway_started(agent_id)

        # ensure-browser must have run exactly once across all three spawns.
        assert _wait_for_counter(counter_path, expected=1) == 1
        # And a log file for ensure-browser got written in the minds data dir.
        assert ensure_browser_log_path(tmp_path).is_file()
    finally:
        for agent_id in agent_ids:
            manager.stop_gateway_for_agent(agent_id)
        manager.stop()


def test_ensure_browser_not_called_when_binary_missing(tmp_path: Path) -> None:
    """If the binary is missing, the manager must raise without trying to
    spawn ``ensure-browser`` (there's nothing to run)."""
    manager = LatchkeyGatewayManager(latchkey_binary=str(tmp_path / "missing"))
    manager.start(data_dir=tmp_path)
    try:
        with pytest.raises(LatchkeyBinaryNotFoundError):
            manager.ensure_gateway_started(AgentId())
        assert not ensure_browser_log_path(tmp_path).exists()
    finally:
        manager.stop()


def test_destruction_handler_stops_gateway(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = LatchkeyGatewayManager(latchkey_binary=str(fake_binary))
    manager.start(data_dir=tmp_path)
    tunnel_manager = _RecordingTunnelManager()
    try:
        discovery = LatchkeyGatewayDiscoveryHandler(gateway_manager=manager, tunnel_manager=tunnel_manager)
        destruction = LatchkeyGatewayDestructionHandler(gateway_manager=manager)
        agent_id = AgentId()
        discovery(agent_id, None, "docker")
        info = manager.get_gateway_info(agent_id)
        assert info is not None

        destruction(agent_id)
        assert manager.get_gateway_info(agent_id) is None
        assert load_gateway_info(tmp_path, agent_id) is None
        assert _wait_for_process_exit(info.pid)
    finally:
        manager.stop()
