import hashlib
import json
import os
import signal
import socket
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyBinaryNotFoundError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.core import LatchkeyNotInitializedError
from imbue.mngr_latchkey.core import _cmdline_looks_like_latchkey_gateway
from imbue.mngr_latchkey.discovery import LatchkeyDestructionHandler
from imbue.mngr_latchkey.discovery import LatchkeyDiscoveryHandler
from imbue.mngr_latchkey.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_latchkey.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.store import LatchkeyGatewayInfo
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import ensure_browser_log_path
from imbue.mngr_latchkey.store import load_gateway_info
from imbue.mngr_latchkey.store import save_gateway_info

_POLL_INTERVAL_SECONDS = 0.05


def test_cmdline_matcher_accepts_plausible_latchkey_gateway() -> None:
    assert _cmdline_looks_like_latchkey_gateway(["/usr/local/bin/latchkey", "gateway"])
    assert _cmdline_looks_like_latchkey_gateway(["latchkey", "gateway", "--verbose"])
    # Shebang rewriting: kernel injects the interpreter ahead of the script path.
    assert _cmdline_looks_like_latchkey_gateway(["/usr/bin/env", "node", "/opt/latchkey/cli", "gateway"])
    assert _cmdline_looks_like_latchkey_gateway(["node", "gateway"]) is False
    assert _cmdline_looks_like_latchkey_gateway(["latchkey", "auth", "set"]) is False
    assert _cmdline_looks_like_latchkey_gateway([]) is False


def test_ensure_gateway_started_requires_initialize(tmp_path: Path) -> None:
    manager = Latchkey(latchkey_directory=tmp_path)
    with pytest.raises(LatchkeyNotInitializedError):
        manager.ensure_gateway_started()


def test_plugin_data_dir_is_subdir_of_latchkey_directory(tmp_path: Path) -> None:
    """Plugin metadata always lives in ``<latchkey_directory>/mngr_latchkey/``."""
    manager = Latchkey(latchkey_directory=tmp_path)
    assert manager.plugin_data_dir == tmp_path / "mngr_latchkey"


def test_ensure_gateway_started_raises_when_binary_missing(tmp_path: Path) -> None:
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "definitely-does-not-exist"))
    manager.initialize()
    with pytest.raises(LatchkeyBinaryNotFoundError):
        manager.ensure_gateway_started()


def _make_fake_latchkey_binary(tmp_path: Path) -> Path:
    """Build a shell script that imitates ``latchkey`` for gateway / ensure-browser / create-jwt.

    ``gateway`` binds a TCP socket on the host:port supplied via env vars
    and sleeps until terminated. ``ensure-browser`` exits immediately.
    ``gateway create-jwt`` emits a stable token that depends on the
    requested ``permissions_config_path`` -- enough for the manager to
    verify password-derivation and JWT-minting end to end without
    running a real Latchkey.
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
    #
    # ``gateway create-jwt`` is also handled here so the manager's
    # password-derivation and per-agent JWT-minting paths can be
    # exercised against this fake binary without a full Latchkey
    # install. The ``token`` we emit is just a deterministic function
    # of the requested file path -- it is not a real JWT, but it is
    # all the manager needs (it just hashes the password sentinel and
    # forwards the per-agent value to the agent).
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}' if args else 'fake-jwt')\n"
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


def test_ensure_gateway_started_spawns_subprocess_and_persists_record(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    try:
        info = manager.ensure_gateway_started()
        assert info.host == "127.0.0.1"
        assert info.port > 0
        assert info.pid > 0
        assert _wait_for_listening(info.host, info.port), "gateway did not start listening"

        # The record was persisted and matches the returned info.
        record = load_gateway_info(manager.plugin_data_dir)
        assert record is not None
        assert record.host == info.host
        assert record.port == info.port
        assert record.pid == info.pid

        # Idempotent: a second call returns the same info without spawning again.
        second = manager.ensure_gateway_started()
        assert second == info
        assert manager.get_gateway_info() == info
    finally:
        manager.stop_gateway()


def test_ensure_gateway_started_materializes_deny_all_default_permissions(tmp_path: Path) -> None:
    """The default permissions file must exist with empty rules before the gateway starts.

    Latchkey treats a missing permissions file as ``allow all``, so we
    materialize a deny-all baseline up front. This file is what the
    gateway consults when an incoming request does not present a valid
    permissions-override JWT, so it must not be permissive.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    perms_path = default_permissions_path(manager.plugin_data_dir)
    assert not perms_path.exists()
    try:
        info = manager.ensure_gateway_started()
        assert _wait_for_listening(info.host, info.port)
        assert perms_path.is_file()
        assert json.loads(perms_path.read_text()) == {"rules": []}
    finally:
        manager.stop_gateway()


def test_ensure_gateway_started_preserves_existing_default_permissions_file(tmp_path: Path) -> None:
    """An existing default permissions file must not be overwritten on spawn."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    perms_path = default_permissions_path(manager.plugin_data_dir)
    perms_path.parent.mkdir(parents=True, exist_ok=True)
    existing = '{"rules": [{"some-scope": ["any"]}]}'
    perms_path.write_text(existing)
    try:
        info = manager.ensure_gateway_started()
        assert _wait_for_listening(info.host, info.port)
        assert perms_path.read_text() == existing
    finally:
        manager.stop_gateway()


def test_restart_adopts_live_gateway_and_discards_stale_info(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    # First "session": start a gateway. We don't tear down ``manager_a``
    # -- in production the desktop client just exits and the detached
    # gateway keeps running. The second-session manager below must adopt
    # the surviving subprocess from the on-disk record alone.
    manager_a = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager_a.initialize()
    info = manager_a.ensure_gateway_started()
    assert _wait_for_listening(info.host, info.port)

    try:
        # Second "session": manager should adopt the live record.
        manager_b = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
        manager_b.initialize()
        try:
            adopted = manager_b.get_gateway_info()
            assert adopted is not None
            assert adopted.pid == info.pid
            assert adopted.port == info.port

            # A second ensure_gateway_started should reuse the adopted
            # process -- no new PID allocated.
            ensured = manager_b.ensure_gateway_started()
            assert ensured.pid == info.pid
        finally:
            manager_b.stop_gateway()
    finally:
        if psutil.pid_exists(info.pid):
            try:
                os.kill(info.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_initialize_discards_stale_record(tmp_path: Path) -> None:
    """A persisted record whose PID is gone is discarded on initialize."""
    stale_info = LatchkeyGatewayInfo(
        host="127.0.0.1",
        port=1,
        pid=2**31 - 1,
        started_at=datetime.now(timezone.utc),
    )
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "missing"))
    save_gateway_info(manager.plugin_data_dir, stale_info)
    manager.initialize()
    assert manager.get_gateway_info() is None
    assert load_gateway_info(manager.plugin_data_dir) is None


def test_concurrent_ensure_gateway_started_spawns_at_most_one_subprocess(tmp_path: Path) -> None:
    """Two threads racing through ``ensure_gateway_started`` must not both spawn.

    Without the spawn lock, both callers would observe ``_info`` as
    ``None``, both would proceed to spawn a real subprocess, and the
    second write to ``_info`` would leak the loser's process. We
    detect that by counting how many distinct PIDs the manager hands
    back across many concurrent callers and how many ``latchkey``
    invocations reached the binary.
    """
    invocation_counter = tmp_path / "gateway_invocations"
    script = tmp_path / "latchkey"
    # The fake binary records every invocation that hits the
    # ``gateway`` subcommand (not ``ensure-browser`` / ``create-jwt``,
    # which are unrelated bookkeeping) and then sleeps briefly before
    # binding its port. The artificial delay widens the race window
    # so the test reliably catches a regression to the no-lock
    # behaviour.
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys, time\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}')\n"
        "    sys.exit(0)\n"
        'assert sys.argv[1] == "gateway"\n'
        f"open({str(invocation_counter)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        "time.sleep(0.5)\n"
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

    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()

    barrier = threading.Barrier(8)
    results: list[LatchkeyGatewayInfo] = []
    results_lock = threading.Lock()

    def worker() -> None:
        # Sync all workers so they all attempt the spawn at roughly
        # the same instant. This maximises the chance of catching a
        # regression to the no-lock behaviour.
        barrier.wait()
        info = manager.ensure_gateway_started()
        with results_lock:
            results.append(info)

    threads = [threading.Thread(target=worker, name=f"spawn-race-{i}") for i in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        # All callers must agree on a single gateway info.
        assert len({(info.pid, info.port) for info in results}) == 1, results
        # And the fake binary must have been invoked exactly once for
        # the ``gateway`` subcommand. (``ensure-browser`` and
        # ``create-jwt`` invocations are short-circuited above and do
        # not write to this file.)
        assert invocation_counter.is_file()
        invocations = invocation_counter.read_text().splitlines()
        assert len(invocations) == 1, f"expected one gateway spawn, got {invocations}"
    finally:
        manager.stop_gateway()


def test_stop_gateway_terminates_subprocess_and_removes_record(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    info = manager.ensure_gateway_started()
    assert _wait_for_listening(info.host, info.port)

    manager.stop_gateway()
    assert manager.get_gateway_info() is None
    assert load_gateway_info(manager.plugin_data_dir) is None
    assert _wait_for_process_exit(info.pid)


def test_stop_gateway_is_no_op_when_not_running(tmp_path: Path) -> None:
    manager = Latchkey(latchkey_directory=tmp_path)
    manager.initialize()
    manager.stop_gateway()


def test_derive_gateway_password_returns_sha256_of_sentinel_jwt(tmp_path: Path) -> None:
    """The password is the SHA-256 hex of the sentinel-path JWT."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()

    # The fake binary emits ``fake-jwt-for:<path>``; we don't care
    # about the exact path here, only that we got a stable hex digest
    # of it.
    password = manager.derive_gateway_password()
    # SHA-256 hex digest is 64 hex characters.
    assert len(password) == 64
    # Must parse as hexadecimal -- raises ValueError otherwise.
    int(password, 16)

    # Cached: a second call returns the same value without re-running.
    assert manager.derive_gateway_password() == password
    # And the digest matches what we'd get by hashing the JWT directly.
    expected = hashlib.sha256(b"fake-jwt-for:/__minds_gateway_password__/sentinel").hexdigest()
    assert expected == password


def test_derive_gateway_password_propagates_failure(tmp_path: Path) -> None:
    """A failed ``gateway create-jwt`` must surface as ``LatchkeyJwtMintError``."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('No encryption key available.\\n')\nsys.exit(1)\n"
    )
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    with pytest.raises(LatchkeyJwtMintError):
        manager.derive_gateway_password()


def test_create_permissions_override_jwt_returns_stripped_stdout(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()

    permissions_path = tmp_path / "agents" / str(AgentId()) / "latchkey_permissions.json"
    jwt = manager.create_permissions_override_jwt(permissions_path)
    assert jwt == f"fake-jwt-for:{permissions_path}"


def test_create_permissions_override_jwt_propagates_failure(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(2)\n")
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    with pytest.raises(LatchkeyJwtMintError):
        manager.create_permissions_override_jwt(tmp_path / "perms.json")


def test_create_permissions_override_jwt_clears_latchkey_gateway_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI refuses ``gateway create-jwt`` when ``LATCHKEY_GATEWAY`` is set.

    The desktop client must scrub the env var from any process it
    spawns for create-jwt so the command works regardless of how the
    user's shell is configured.
    """
    monkeypatch.setenv("LATCHKEY_GATEWAY", "http://127.0.0.1:1989")
    report_path = tmp_path / "report"
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"open({str(report_path)!r}, 'w').write(os.environ.get('LATCHKEY_GATEWAY', '<unset>'))\n"
        "print('jwt')\n"
    )
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    manager.create_permissions_override_jwt(tmp_path / "perms.json")
    assert report_path.read_text() == "<unset>"


def test_ensure_gateway_started_passes_password_to_subprocess(tmp_path: Path) -> None:
    """The spawned gateway must receive the derived password as ``LATCHKEY_GATEWAY_LISTEN_PASSWORD``."""
    report_path = tmp_path / "password_report"
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}')\n"
        "    sys.exit(0)\n"
        'assert sys.argv[1] == "gateway"\n'
        "host = os.environ['LATCHKEY_GATEWAY_LISTEN_HOST']\n"
        "port = int(os.environ['LATCHKEY_GATEWAY_LISTEN_PORT'])\n"
        "password = os.environ.get('LATCHKEY_GATEWAY_LISTEN_PASSWORD', '<unset>')\n"
        f"open({str(report_path)!r}, 'w').write(password)\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind((host, port))\n"
        "sock.listen(128)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)

    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    try:
        info = manager.ensure_gateway_started()
        assert _wait_for_listening(info.host, info.port)
        # Wait for the report file to be written.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not report_path.is_file():
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
        assert report_path.is_file()
        assert report_path.read_text() == manager.derive_gateway_password()
    finally:
        manager.stop_gateway()


# -- Discovery handler --


def test_discovery_handler_spawns_shared_gateway_for_every_provider(tmp_path: Path) -> None:
    """Every provider triggers the shared gateway to start; a second call is a no-op."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = SSHTunnelManager()
    try:
        with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
            handler = LatchkeyDiscoveryHandler(
                latchkey=manager,
                tunnel_manager=tunnel_manager,
                concurrency_group=cg,
            )
            for provider_name in ("local", "docker", "lima", "vultr", "modal"):
                # ssh_info=None is fine here -- it keeps the test off the SSH path.
                handler(AgentId(), None, provider_name)
        info = manager.get_gateway_info()
        assert info is not None
        # Same shared gateway across all five callbacks; ensure it actually came up.
        assert _wait_for_listening(info.host, info.port)
    finally:
        manager.stop_gateway()
        tunnel_manager.cleanup()


class _RecordingTunnelManager(SSHTunnelManager):
    """SSHTunnelManager that records setup/remove calls instead of doing SSH."""

    _calls: list[tuple[RemoteSSHInfo, int, int, str | None]] = PrivateAttr(default_factory=list)
    _removed_agent_ids: list[str] = PrivateAttr(default_factory=list)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
        agent_id: str | None = None,
    ) -> int:
        self._calls.append((ssh_info, local_port, remote_port, agent_id))
        return remote_port

    def remove_reverse_tunnels_for_agent(self, agent_id: str) -> int:
        self._removed_agent_ids.append(agent_id)
        return 0


def test_discovery_handler_sets_up_reverse_tunnel_when_ssh_info_given(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    agent_id = AgentId()
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=22, key_path=tmp_path / "k")
    try:
        # The handler dispatches tunnel setup onto a CG worker thread, so
        # exit the CG (joining its threads) before asserting on the
        # recording tunnel manager's calls -- otherwise the assertion races
        # the worker.
        with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
            handler = LatchkeyDiscoveryHandler(
                latchkey=manager,
                tunnel_manager=tunnel_manager,
                concurrency_group=cg,
            )
            handler(agent_id, ssh_info, "docker")

        info = manager.get_gateway_info()
        assert info is not None

        # Exactly one reverse tunnel, bridging the dynamic host-side gateway port
        # to the fixed agent-side port on the container's loopback. The tunnel
        # must also be tagged with the owning agent's id, so the destruction
        # handler can find and tear it down via remove_reverse_tunnels_for_agent;
        # without that tag the original CPU leak would re-surface.
        assert tunnel_manager._calls == [(ssh_info, info.port, AGENT_SIDE_LATCHKEY_PORT, str(agent_id))]
    finally:
        manager.stop_gateway()


def test_discovery_handler_skips_reverse_tunnel_when_ssh_info_missing(tmp_path: Path) -> None:
    """Agents discovered without SSH info skip reverse-tunnel setup.

    Without an SSH route the handler cannot forward the host-side gateway
    into the agent, so it just ensures the gateway is up and returns.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    try:
        with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
            handler = LatchkeyDiscoveryHandler(
                latchkey=manager,
                tunnel_manager=tunnel_manager,
                concurrency_group=cg,
            )
            handler(AgentId(), None, "local")

        assert manager.get_gateway_info() is not None
        assert tunnel_manager._calls == []
    finally:
        manager.stop_gateway()


def test_discovery_handler_swallows_gateway_errors(tmp_path: Path) -> None:
    """A missing binary must not crash the discovery callback -- just log a warning."""
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "missing"))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = LatchkeyDiscoveryHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
        )
        handler(AgentId(), None, "local")
    assert manager.get_gateway_info() is None
    assert tunnel_manager._calls == []


def _make_fake_latchkey_binary_with_ensure_browser_counter(tmp_path: Path, counter_path: Path) -> Path:
    """Build a fake ``latchkey`` that handles ``gateway`` (blocking),
    ``gateway create-jwt`` (deterministic stub), and ``ensure-browser``
    (increments ``counter_path``).

    Lets us verify that the manager calls ``ensure-browser`` exactly once per
    session regardless of how many times the gateway gets spawned.
    """
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    counter_path = os.environ['FAKE_LATCHKEY_COUNTER']\n"
        "    open(counter_path, 'a').write('1\\n')\n"
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}')\n"
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
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    try:
        # Multiple ensure_gateway_started calls -- the gateway is shared.
        for _ in range(3):
            manager.ensure_gateway_started()

        # ensure-browser must have run exactly once.
        assert _wait_for_counter(counter_path, expected=1) == 1
        # And a log file for ensure-browser got written in the minds data dir.
        assert ensure_browser_log_path(manager.plugin_data_dir).is_file()
    finally:
        manager.stop_gateway()


def test_ensure_browser_not_called_when_binary_missing(tmp_path: Path) -> None:
    """If the binary is missing, the manager must raise without trying to
    spawn ``ensure-browser`` (there's nothing to run)."""
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "missing"))
    manager.initialize()
    with pytest.raises(LatchkeyBinaryNotFoundError):
        manager.ensure_gateway_started()
    assert not ensure_browser_log_path(manager.plugin_data_dir).exists()


# -- Destruction handler --


def test_destruction_handler_removes_reverse_tunnels_for_destroyed_agent() -> None:
    """The handler must ask the tunnel manager to drop the destroyed agent's
    reverse tunnels. The shared gateway must NOT be touched -- it serves
    other agents.
    """
    tunnel_manager = _RecordingTunnelManager()
    handler = LatchkeyDestructionHandler(tunnel_manager=tunnel_manager)
    agent_id = AgentId()
    handler(agent_id)
    assert tunnel_manager._removed_agent_ids == [str(agent_id)]


# -- services_info / auth_browser --


def _make_services_info_binary(
    tmp_path: Path,
    *,
    credential_status: str = "valid",
    exit_code: int = 0,
) -> Path:
    """Build a fake latchkey CLI that emits a services-info JSON payload."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"if sys.argv[1:3] != ['services', 'info']:\n"
        f"    print('unexpected args:', sys.argv, file=sys.stderr)\n"
        f"    sys.exit(99)\n"
        f"payload = {{\n"
        f'    "type": "built-in",\n'
        f'    "baseApiUrls": ["https://api.example.com"],\n'
        f'    "authOptions": ["browser", "set"],\n'
        f'    "credentialStatus": {credential_status!r},\n'
        f'    "setCredentialsExample": "...",\n'
        f'    "developerNotes": "...",\n'
        f"}}\n"
        f"print(json.dumps(payload, indent=2))\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def _make_recording_binary(tmp_path: Path, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Build a fake latchkey CLI that records its argv and the LATCHKEY_DIRECTORY env var."""
    script = tmp_path / "latchkey"
    report_path = tmp_path / "latchkey_report.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(report_path)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"if {stderr!r}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def test_services_info_returns_valid_when_status_is_valid(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="valid")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    info = latchkey.services_info("slack")
    assert info.credential_status == CredentialStatus.VALID
    assert info.auth_options == frozenset({"browser", "set"})
    assert info.set_credentials_example == "..."


def test_services_info_returns_missing_when_status_is_missing(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="missing")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    assert latchkey.services_info("slack").credential_status == CredentialStatus.MISSING


def test_services_info_returns_invalid_when_status_is_invalid(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="invalid")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    assert latchkey.services_info("slack").credential_status == CredentialStatus.INVALID


def test_services_info_returns_unknown_when_process_fails(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, exit_code=1)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    info = latchkey.services_info("slack")
    assert info.credential_status == CredentialStatus.UNKNOWN
    assert info.auth_options == frozenset()
    assert info.set_credentials_example is None


def test_services_info_returns_unknown_when_binary_does_not_exist(tmp_path: Path) -> None:
    """Missing latchkey binary must degrade to UNKNOWN, not crash callers (e.g. dialog render)."""
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "does-not-exist"))
    info = latchkey.services_info("slack")
    assert info.credential_status == CredentialStatus.UNKNOWN
    assert info.auth_options == frozenset()
    assert info.set_credentials_example is None


def test_services_info_returns_unknown_when_output_is_not_json(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text("#!/usr/bin/env python3\nprint('not json')\n")
    script.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    info = latchkey.services_info("slack")
    assert info.credential_status == CredentialStatus.UNKNOWN
    assert info.auth_options == frozenset()
    assert info.set_credentials_example is None


def test_services_info_returns_unknown_for_unrecognized_status(tmp_path: Path) -> None:
    binary = _make_services_info_binary(tmp_path, credential_status="totally-new")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    assert latchkey.services_info("slack").credential_status == CredentialStatus.UNKNOWN


def test_services_info_returns_empty_auth_options_when_field_is_missing(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'credentialStatus': 'missing'}))\n")
    script.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    info = latchkey.services_info("coolify")
    assert info.credential_status == CredentialStatus.MISSING
    assert info.auth_options == frozenset()
    assert info.set_credentials_example is None


def test_services_info_returns_set_only_auth_options_for_set_only_service(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({\n"
        "    'credentialStatus': 'missing',\n"
        "    'authOptions': ['set'],\n"
        "    'setCredentialsExample': 'latchkey auth set coolify -H \"Authorization: Bearer <token>\"',\n"
        "}))\n"
    )
    script.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    info = latchkey.services_info("coolify")
    assert info.credential_status == CredentialStatus.MISSING
    assert info.auth_options == frozenset({"set"})
    assert info.set_credentials_example is not None
    assert "latchkey auth set coolify" in info.set_credentials_example


def test_services_info_skips_malformed_auth_options(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "print(json.dumps({'credentialStatus': 'missing', 'authOptions': 'browser'}))\n"
    )
    script.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    info = latchkey.services_info("slack")
    assert info.credential_status == CredentialStatus.MISSING
    assert info.auth_options == frozenset()


def test_services_info_passes_latchkey_directory_through(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    report_path = tmp_path / "report"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"with open({str(report_path)!r}, 'w') as f:\n"
        "    f.write(os.environ.get('LATCHKEY_DIRECTORY', ''))\n"
        "print(json.dumps({'credentialStatus': 'valid'}))\n"
    )
    script.chmod(0o755)
    latchkey_dir = tmp_path / "shared_latchkey"

    latchkey = Latchkey(latchkey_directory=latchkey_dir, latchkey_binary=str(script))
    latchkey.services_info("slack")

    assert report_path.read_text() == str(latchkey_dir)


def test_auth_browser_reports_success_on_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=0)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("slack")

    assert is_success is True
    assert detail == ""


def test_auth_browser_reports_failure_on_non_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=1, stderr="user cancelled")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("slack")

    assert is_success is False
    assert detail == "user cancelled"


def test_auth_browser_uses_auth_browser_subcommand(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=0)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    latchkey.auth_browser("slack")

    report_path = tmp_path / "latchkey_report.jsonl"
    line = report_path.read_text().strip()
    record = json.loads(line)
    assert record == {"argv": ["auth", "browser", "slack"], "env_LATCHKEY_DIRECTORY": str(tmp_path)}
