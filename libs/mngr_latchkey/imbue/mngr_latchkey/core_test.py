import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
from loguru import logger
from pydantic import PrivateAttr
from watchdog.events import FileModifiedEvent
from watchdog.observers import Observer

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyBinaryNotFoundError
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.core import LatchkeyNotInitializedError
from imbue.mngr_latchkey.core import LatchkeyVersionError
from imbue.mngr_latchkey.core import MINDS_GOOGLE_OAUTH_CLIENT_ID
from imbue.mngr_latchkey.core import MINDS_GOOGLE_OAUTH_CLIENT_SECRET
from imbue.mngr_latchkey.core import MINDS_GOOGLE_OAUTH_SERVICES
from imbue.mngr_latchkey.core import _log_gateway_output_line
from imbue.mngr_latchkey.discovery import LatchkeyDestructionHandler
from imbue.mngr_latchkey.discovery import LatchkeyDiscoveryHandler
from imbue.mngr_latchkey.discovery import _LatchkeyStateChangeHandler
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote_gateway import RemoteGatewayError
from imbue.mngr_latchkey.remote_gateway import local_credentials_path
from imbue.mngr_latchkey.store import admin_permissions_path
from imbue.mngr_latchkey.store import default_permissions_path
from imbue.mngr_latchkey.store import ensure_browser_log_path
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.testing import FakeLatchkey

_POLL_INTERVAL_SECONDS = 0.05


def test_gateway_output_is_routed_through_loguru() -> None:
    """Gateway output lines are emitted as structured loguru events (not a raw file).

    This is what folds the gateway's otherwise-unstructured output into the
    supervisor's standard rotating, timestamped JSONL log.
    """
    captured: list[tuple[str, str]] = []

    def _sink(message: object) -> None:
        record = message.record  # ty: ignore[unresolved-attribute]
        captured.append((record["level"].name, record["message"]))

    handler_id = logger.add(_sink, level="DEBUG", format="{message}")
    try:
        _log_gateway_output_line("hello from the gateway\n", is_stdout=True)
    finally:
        logger.remove(handler_id)

    assert ("DEBUG", "[latchkey gateway] hello from the gateway") in captured


# The previous on-disk gateway-record tests went away when the record
# itself did -- gateway lifetime is now scoped to a single ``mngr
# latchkey forward`` subprocess. The cmdline-matcher / cross-process
# adoption / stale-record tests below are dropped for the same reason.


def test_start_gateway_requires_initialize(tmp_path: Path) -> None:
    manager = Latchkey(latchkey_directory=tmp_path)
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        with pytest.raises(LatchkeyNotInitializedError):
            manager.start_gateway(cg)


def test_plugin_data_dir_is_subdir_of_latchkey_directory(tmp_path: Path) -> None:
    """Plugin metadata always lives in ``<latchkey_directory>/mngr_latchkey/``."""
    manager = Latchkey(latchkey_directory=tmp_path)
    assert manager.plugin_data_dir == tmp_path / "mngr_latchkey"


def test_initialize_raises_when_binary_missing(tmp_path: Path) -> None:
    """``initialize`` is the first thing to touch the binary (via ``--version``).

    A missing binary surfaces immediately rather than waiting for the
    first ``ensure_gateway_started`` call.
    """
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(tmp_path / "definitely-does-not-exist"))
    with pytest.raises(LatchkeyBinaryNotFoundError):
        manager.initialize()


def test_start_gateway_raises_when_binary_disappears_after_initialize(tmp_path: Path) -> None:
    """``initialize`` succeeded but the binary was removed before spawn.

    The spawn-time binary-missing check inside ``start_gateway`` still
    fires; the version check at ``initialize`` is just an earlier line
    of defence.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    fake_binary.unlink()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        with pytest.raises(LatchkeyBinaryNotFoundError):
            manager.start_gateway(cg)


# -- initialize() version check ----------------------------------------------


def _make_version_binary(tmp_path: Path, version_output: str, exit_code: int = 0) -> Path:
    """Build a stub ``latchkey`` that responds to ``--version`` and nothing else.

    Sufficient for the ``initialize`` version-gate tests, which never
    drive the manager past the version check.
    """
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f'assert sys.argv[1] == "--version", f"unexpected argv: {{sys.argv[1:]!r}}"\n'
        f"print({version_output!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def test_initialize_accepts_exactly_minimum_version(tmp_path: Path) -> None:
    binary = _make_version_binary(tmp_path, version_output=LATCHKEY_MIN_VERSION)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    manager.initialize()


def test_initialize_accepts_newer_version(tmp_path: Path) -> None:
    binary = _make_version_binary(tmp_path, version_output="3.0.0")
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    manager.initialize()


def test_initialize_tolerates_leading_v_prefix(tmp_path: Path) -> None:
    """Some CLIs print ``v<version>`` rather than the bare semver string."""
    binary = _make_version_binary(tmp_path, version_output=f"v{LATCHKEY_MIN_VERSION}")
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    manager.initialize()


def test_initialize_rejects_older_version(tmp_path: Path) -> None:
    binary = _make_version_binary(tmp_path, version_output="2.7.5")
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    with pytest.raises(LatchkeyVersionError) as exc_info:
        manager.initialize()
    # The error message must surface both versions so the user knows what
    # they have and what they need.
    assert "2.7.5" in str(exc_info.value)
    assert LATCHKEY_MIN_VERSION in str(exc_info.value)


def test_initialize_raises_when_version_output_unparseable(tmp_path: Path) -> None:
    binary = _make_version_binary(tmp_path, version_output="this is not a version")
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    with pytest.raises(LatchkeyError) as exc_info:
        manager.initialize()
    # Not a LatchkeyVersionError -- this is parsing failure, distinct from
    # "too old".
    assert not isinstance(exc_info.value, LatchkeyVersionError)


def test_initialize_raises_when_version_command_exits_nonzero(tmp_path: Path) -> None:
    binary = _make_version_binary(tmp_path, version_output="broken", exit_code=1)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))
    with pytest.raises(LatchkeyError):
        manager.initialize()


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
    # ``--version`` is what ``Latchkey.initialize`` runs at startup to
    # gate on the minimum version; emit a string the version-parser is
    # happy with and that satisfies ``LATCHKEY_MIN_VERSION``.
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    args = [a for a in sys.argv[3:] if not a.startswith('--')]\n"
        "    print(f'fake-jwt-for:{args[0]}' if args else 'fake-jwt')\n"
        "    sys.exit(0)\n"
        # ``auth re-encrypt <destination> [service ...]`` writes a fake
        # filtered store as ``credentials.json.enc`` into the <destination>
        # directory, recording the requested services (and that stdin was
        # empty, i.e. the same key is reused). Enough to verify the manager
        # builds the right command and reads the result.
        'if sys.argv[1:3] == ["auth", "re-encrypt"]:\n'
        "    import json as _json, os as _os\n"
        "    destination = sys.argv[3]\n"
        "    rest = sys.argv[4:]\n"
        "    services = rest[1:] if rest[:1] == ['--services'] else []\n"
        "    stdin_key = sys.stdin.read()\n"
        "    payload = {'services': services, 'reused_key': stdin_key == ''}\n"
        "    out = _os.path.join(destination, 'credentials.json.enc')\n"
        "    open(out, 'w').write(_json.dumps(payload))\n"
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


def test_start_gateway_spawns_subprocess(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert manager.is_gateway_running
        assert port > 0
        assert _wait_for_listening("127.0.0.1", port), "gateway did not start listening"

        # In-process idempotent: a second call is a no-op; the port
        # stays the same. Cross-process adoption was removed along
        # with the on-disk gateway record.
        assert manager.start_gateway(cg) == port

        # ``stop_gateway`` must run inside the CG so the long-running
        # gateway subprocess is gone before the CG waits for strands
        # to finish at ``__exit__``.
        manager.stop_gateway()


def test_start_gateway_materializes_deny_all_default_permissions(tmp_path: Path) -> None:
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
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        assert perms_path.is_file()
        assert json.loads(perms_path.read_text()) == {"rules": []}
        manager.stop_gateway()


def test_start_gateway_drops_bundled_extensions(tmp_path: Path) -> None:
    """The gateway spawn step must materialize the bundled .mjs files under LATCHKEY_DIRECTORY/extensions/."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    extensions_dir = tmp_path / "extensions"
    assert not extensions_dir.exists()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        mjs_files = sorted(p.name for p in extensions_dir.iterdir() if p.suffix == ".mjs")
        assert mjs_files == ["minds_api_proxy.mjs", "permission_requests.mjs", "permissions.mjs"]
        # The destination files must be non-empty -- ``importlib.resources``
        # silently produces empty reads if the wheel does not actually
        # ship the .mjs payloads.
        for name in mjs_files:
            assert (extensions_dir / name).read_text().startswith("/**")
        # ``services.json`` ships alongside the .mjs files and must also
        # be materialized so the permissions extension can read it at
        # request time.
        services_json_path = extensions_dir / "services.json"
        assert services_json_path.is_file()
        services_catalog = json.loads(services_json_path.read_text())
        assert isinstance(services_catalog, dict) and len(services_catalog) > 0
        for service_name, entries in services_catalog.items():
            assert isinstance(service_name, str) and len(service_name) > 0
            # Each service maps to a list of scope entries (a service may
            # expose more than one detent scope).
            assert isinstance(entries, list) and len(entries) > 0
            for entry in entries:
                assert {"scope", "display_name", "permissions"} <= set(entry.keys())
                assert isinstance(entry["scope"], str) and len(entry["scope"]) > 0
                assert isinstance(entry["display_name"], str) and len(entry["display_name"]) > 0
                # The scope-level ``description`` carries detent's ``$comment``
                # summary. It is optional -- consumers must not depend on it --
                # so only assert its type when present.
                assert isinstance(entry.get("description", ""), str)
                # Each permission is an object whose ``name`` is required; the
                # ``description`` (detent's ``$comment``) is colocated with it
                # but optional.
                assert isinstance(entry["permissions"], list)
                for permission in entry["permissions"]:
                    assert isinstance(permission["name"], str) and len(permission["name"]) > 0
                    assert isinstance(permission.get("description", ""), str)
        manager.stop_gateway()


def test_start_gateway_overwrites_existing_extensions(tmp_path: Path) -> None:
    """Stale extension content from a prior install must be overwritten on every spawn."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    extensions_dir = tmp_path / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    stale_file = extensions_dir / "permissions.mjs"
    stale_file.write_text("// stale\n")
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        assert stale_file.read_text() != "// stale\n"
        manager.stop_gateway()


def test_create_admin_permissions_jwt_materializes_admin_file(tmp_path: Path) -> None:
    """Calling ``create_admin_permissions_jwt`` materializes the admin file with wildcard rules."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    admin_path = admin_permissions_path(manager.plugin_data_dir)
    assert not admin_path.exists()
    jwt = manager.create_admin_permissions_jwt()
    assert jwt == f"fake-jwt-for:{admin_path}"
    on_disk = json.loads(admin_path.read_text())
    assert on_disk == {"rules": [{"any": ["any"]}]}


def test_create_admin_permissions_jwt_caches_token(tmp_path: Path) -> None:
    """Repeated calls return the cached JWT without re-shelling out."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    first = manager.create_admin_permissions_jwt()
    # Replace the fake binary's create-jwt output mid-run so a second
    # shell-out would be observable; the cache must absorb the call.
    fake_binary.write_text(fake_binary.read_text().replace("fake-jwt-for:", "DIFFERENT:"))
    second = manager.create_admin_permissions_jwt()
    assert first == second


def test_create_admin_permissions_jwt_preserves_existing_admin_file(tmp_path: Path) -> None:
    """A pre-existing admin permissions file is not overwritten -- the user's edits survive."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    admin_path = admin_permissions_path(manager.plugin_data_dir)
    admin_path.parent.mkdir(parents=True, exist_ok=True)
    custom = '{"rules": [{"custom-scope": ["custom-perm"]}]}'
    admin_path.write_text(custom)
    manager.create_admin_permissions_jwt()
    assert admin_path.read_text() == custom


def test_start_gateway_sets_extension_permissions_root_env_var(tmp_path: Path) -> None:
    """The spawned gateway must see LATCHKEY_EXTENSION_PERMISSIONS_ROOT pointing at the plugin data dir."""
    script = tmp_path / "latchkey"
    env_dump_path = tmp_path / "env_dump"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    sys.exit(0)\n"
        'if sys.argv[1:3] == ["gateway", "create-jwt"]:\n'
        "    print('fake-jwt')\n"
        "    sys.exit(0)\n"
        "with open(" + repr(str(env_dump_path)) + ", 'w') as fh:\n"
        "    fh.write(os.environ.get('LATCHKEY_EXTENSION_PERMISSIONS_ROOT', ''))\n"
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
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        # The child process writes the env value to env_dump_path as
        # one of its first acts; poll briefly for the file.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not env_dump_path.is_file():
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
        assert env_dump_path.is_file()
        assert env_dump_path.read_text() == str(manager.plugin_data_dir)
        manager.stop_gateway()


def test_start_gateway_preserves_existing_default_permissions_file(tmp_path: Path) -> None:
    """An existing default permissions file must not be overwritten on spawn."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    perms_path = default_permissions_path(manager.plugin_data_dir)
    perms_path.parent.mkdir(parents=True, exist_ok=True)
    existing = '{"rules": [{"some-scope": ["any"]}]}'
    perms_path.write_text(existing)
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        assert perms_path.read_text() == existing
        manager.stop_gateway()


def test_concurrent_start_gateway_spawns_at_most_one_subprocess(tmp_path: Path) -> None:
    """Two threads racing through ``start_gateway`` must not both spawn.

    Without the spawn lock, both callers would observe the in-memory
    "not running" flag, both would proceed to spawn a real
    subprocess, and the second write would leak the loser's process.
    We detect that by counting how many ``latchkey`` invocations
    reached the binary across many concurrent callers.
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
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
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
    observed_ports: list[int] = []
    observed_lock = threading.Lock()

    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:

        def worker() -> None:
            # Sync all workers so they all attempt the spawn at roughly
            # the same instant. This maximises the chance of catching a
            # regression to the no-lock behaviour.
            barrier.wait()
            port = manager.start_gateway(cg)
            with observed_lock:
                observed_ports.append(port)

        threads = [threading.Thread(target=worker, name=f"spawn-race-{i}") for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        # All callers must agree on a single gateway port.
        assert len(set(observed_ports)) == 1, observed_ports
        # And the fake binary must have been invoked exactly once for
        # the ``gateway`` subcommand. (``ensure-browser`` and
        # ``create-jwt`` invocations are short-circuited above and do
        # not write to this file.)
        assert invocation_counter.is_file()
        invocations = invocation_counter.read_text().splitlines()
        assert len(invocations) == 1, f"expected one gateway spawn, got {invocations}"
        manager.stop_gateway()


def test_stop_gateway_clears_in_memory_state(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert manager.is_gateway_running
        assert _wait_for_listening("127.0.0.1", port)

        manager.stop_gateway()
        assert not manager.is_gateway_running
        # Idempotent no-op so the CG has nothing left to wait for when it exits.
        manager.stop_gateway()


def test_stop_gateway_is_no_op_when_not_running(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
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
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('No encryption key available.\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    with pytest.raises(LatchkeyJwtMintError):
        manager.derive_gateway_password()


def test_export_credentials_subset_passes_sorted_services_and_reuses_key(tmp_path: Path) -> None:
    """The filtered export lists the services (sorted) and reuses the key (empty stdin)."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    destination = tmp_path / "subset"
    destination.mkdir()

    manager.export_credentials_subset(destination, {"slack", "github", "discord"})

    payload = json.loads((destination / "credentials.json.enc").read_text())
    # Sorted for a deterministic command line.
    assert payload["services"] == ["discord", "github", "slack"]
    # Empty stdin (DEVNULL) means the same encryption key is reused.
    assert payload["reused_key"] is True


def test_export_credentials_subset_rejects_empty_service_set(tmp_path: Path) -> None:
    """``--services`` requires at least one service, so an empty set is refused."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    destination = tmp_path / "subset.json.enc"

    with pytest.raises(LatchkeyError, match="at least one service"):
        manager.export_credentials_subset(destination, frozenset())
    # Nothing was written: we never invoked the binary.
    assert not destination.exists()


def test_export_credentials_subset_raises_on_failure(tmp_path: Path) -> None:
    """A non-zero ``auth re-encrypt`` exit must surface as ``LatchkeyError``."""
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('boom\\n')\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    with pytest.raises(LatchkeyError, match="re-encrypt"):
        manager.export_credentials_subset(tmp_path / "out.enc", {"slack"})


def test_create_permissions_override_jwt_returns_stripped_stdout(tmp_path: Path) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()

    permissions_path = tmp_path / "agents" / str(AgentId()) / "latchkey_permissions.json"
    jwt = manager.create_permissions_override_jwt(permissions_path)
    assert jwt == f"fake-jwt-for:{permissions_path}"


def test_create_permissions_override_jwt_propagates_failure(tmp_path: Path) -> None:
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        "sys.exit(2)\n"
    )
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
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        f"open({str(report_path)!r}, 'w').write(os.environ.get('LATCHKEY_GATEWAY', '<unset>'))\n"
        "print('jwt')\n"
    )
    script.chmod(0o755)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))
    manager.initialize()
    manager.create_permissions_override_jwt(tmp_path / "perms.json")
    assert report_path.read_text() == "<unset>"


def test_start_gateway_passes_password_to_subprocess(tmp_path: Path) -> None:
    """The spawned gateway must receive the derived password as ``LATCHKEY_GATEWAY_LISTEN_PASSWORD``."""
    report_path = tmp_path / "password_report"
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, socket, signal, sys\n"
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
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
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        port = manager.start_gateway(cg)
        assert _wait_for_listening("127.0.0.1", port)
        # Wait for the report file to be written.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not report_path.is_file():
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
        assert report_path.is_file()
        assert report_path.read_text() == manager.derive_gateway_password()
        manager.stop_gateway()


# -- Discovery handler --


def test_discovery_handler_spawns_shared_gateway_for_every_provider(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Every provider triggers the shared gateway to start; a second call is a no-op."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = SSHTunnelManager()
    # The CG owns the gateway subprocess: it must see the gateway
    # already-stopped before its ``__exit__`` runs, otherwise the CG
    # will time out waiting for the long-running gateway to exit
    # naturally. We call ``stop_gateway`` + ``tunnel_manager.cleanup()``
    # inside the ``with`` block.
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        try:
            handler = LatchkeyDiscoveryHandler(
                latchkey=manager,
                tunnel_manager=tunnel_manager,
                concurrency_group=cg,
                mngr_ctx=temp_mngr_ctx,
            )
            for provider_name in ("local", "docker", "lima", "vultr", "modal"):
                # ssh_info=None is fine here -- it keeps the test off the SSH path.
                handler(AgentId(), HostId(), None, provider_name)
            assert manager.is_gateway_running
            # Same shared gateway across all five callbacks; ensure it actually came up.
            # ``start_gateway`` is idempotent and returns the bound port even
            # when the gateway is already running, so the test can use it as
            # the supported way to read the live port.
            assert _wait_for_listening("127.0.0.1", manager.start_gateway(cg))
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


class _RaisingTunnelManager(SSHTunnelManager):
    """SSHTunnelManager whose reverse-tunnel setup always fails (no SSH)."""

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
        agent_id: str | None = None,
    ) -> int:
        raise SSHTunnelError("simulated reverse-tunnel failure")

    def remove_reverse_tunnels_for_agent(self, agent_id: str) -> int:
        return 0


def test_discovery_handler_sets_up_reverse_tunnel_when_ssh_info_given(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    agent_id = AgentId()
    # The ``local`` provider has no outer host, so the handler falls back to the
    # desktop-side reverse tunnel (rather than the VPS-resident gateway path).
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=22, key_path=tmp_path / "k")
    # The handler dispatches tunnel setup onto a CG worker thread, so
    # exit the CG (joining its threads) before asserting on the
    # recording tunnel manager's calls -- otherwise the assertion races
    # the worker. ``stop_gateway`` must run before the CG exits so the
    # long-running gateway subprocess isn't a strand the CG times out on.
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = LatchkeyDiscoveryHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        handler(agent_id, HostId(), ssh_info, "local")

        assert manager.is_gateway_running
        # ``start_gateway`` is idempotent and returns the bound port even
        # when the gateway is already running, so the test can use it as
        # the supported way to read the live port.
        host_side_port = manager.start_gateway(cg)

        # Tunnel setup runs on a CG worker thread (now behind a provider-lookup
        # that resolves the local provider has no outer host), so poll until it
        # records before asserting rather than racing the worker.
        _poll_event = threading.Event()
        _deadline = time.monotonic() + 5.0
        while time.monotonic() < _deadline and not tunnel_manager._calls:
            _poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)

        # Exactly one reverse tunnel, bridging the dynamic host-side gateway port
        # to the fixed agent-side port on the container's loopback. The tunnel
        # must also be tagged with the owning agent's id, so the destruction
        # handler can find and tear it down via remove_reverse_tunnels_for_agent;
        # without that tag the original CPU leak would re-surface.
        assert tunnel_manager._calls == [(ssh_info, host_side_port, AGENT_SIDE_LATCHKEY_PORT, str(agent_id))]

        manager.stop_gateway()


def test_discovery_handler_skips_reverse_tunnel_when_ssh_info_missing(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """Agents discovered without SSH info skip reverse-tunnel setup.

    Without an SSH route the handler cannot forward the host-side gateway
    into the agent, so it just ensures the gateway is up and returns.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = LatchkeyDiscoveryHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        handler(AgentId(), HostId(), None, "local")

        assert manager.is_gateway_running
        assert tunnel_manager._calls == []
        manager.stop_gateway()


def test_discovery_handler_swallows_gateway_errors(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A missing binary must not crash the discovery callback -- just log a warning."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    # Remove the binary so the discovery handler's call to
    # ``start_gateway`` fails with ``LatchkeyBinaryNotFoundError`` at
    # spawn time, exercising the handler's swallow-and-warn path.
    fake_binary.unlink()
    tunnel_manager = _RecordingTunnelManager()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = LatchkeyDiscoveryHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        handler(AgentId(), HostId(), None, "local")
    assert not manager.is_gateway_running
    assert tunnel_manager._calls == []


class _ProvisionRecordingHandler(LatchkeyDiscoveryHandler):
    """Handler stub that forces the VPS branch and records provisioning instead of running it."""

    _provisioned: list[tuple[AgentId, HostId]] = PrivateAttr(default_factory=list)

    def _host_has_outer_host(self, host_id: HostId, provider_name: str) -> bool:
        return True

    def _run_remote_gateway_provisioning(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo,
        provider_name: str,
    ) -> None:
        try:
            self._provisioned.append((agent_id, host_id))
            with self._remote_hosts_lock:
                self._provisioned_hosts.add(str(host_id))
        finally:
            with self._remote_hosts_lock:
                self._provisioning_hosts.discard(str(host_id))
            with self._pending_lock:
                self._pending_remote_agents.discard(str(agent_id))


def test_discovery_handler_dispatches_vps_provisioning_in_addition_to_desktop_tunnel(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A VPS agent gets BOTH the desktop reverse tunnel and the VPS-resident gateway provisioning."""
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    agent_id = AgentId()
    host_id = HostId()
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=2222, key_path=tmp_path / "k")
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _ProvisionRecordingHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        handler(agent_id, host_id, ssh_info, "imbue_cloud")
        host_side_port = manager.start_gateway(cg)

        # Provisioning runs on its own fire-and-forget CG thread; poll for it.
        poll_event = threading.Event()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not handler._provisioned:
            poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)

        # Both paths ran: the desktop gateway is reverse-tunneled onto the
        # agent-side port AND the VPS-resident gateway provisioning was dispatched.
        assert tunnel_manager._calls == [(ssh_info, host_side_port, AGENT_SIDE_LATCHKEY_PORT, str(agent_id))]
        assert handler._provisioned == [(agent_id, host_id)]
        manager.stop_gateway()


def test_discovery_handler_dispatches_vps_provisioning_when_desktop_tunnel_fails(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """A desktop-side reverse-tunnel failure must not prevent VPS-resident gateway provisioning.

    The two paths are independent (the agent reaches the desktop gateway on
    ``AGENT_SIDE_LATCHKEY_PORT`` and the VPS gateway on ``INNER_PORT`` at once),
    so a failing desktop tunnel still leaves the VPS provisioning dispatched.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RaisingTunnelManager()
    agent_id = AgentId()
    host_id = HostId()
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=2222, key_path=tmp_path / "k")
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _ProvisionRecordingHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        handler(agent_id, host_id, ssh_info, "imbue_cloud")

        # Provisioning runs on its own fire-and-forget CG thread; poll for it.
        poll_event = threading.Event()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not handler._provisioned:
            poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)

        # The desktop tunnel raised, yet the VPS provisioning was still dispatched.
        assert handler._provisioned == [(agent_id, host_id)]
        manager.stop_gateway()


def test_provisioning_coalesces_when_host_pass_already_in_flight(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A second agent on a host whose provisioning is already in flight is coalesced.

    Provisioning is host-scoped (one container, one gateway, one tunnel), so a
    concurrent second pass for another agent on the same host would be redundant
    and race the first on the same VPS files; the per-host in-flight guard
    coalesces it away instead.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    host_id = HostId()
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=2222, key_path=tmp_path / "k")
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _ProvisionRecordingHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        # Simulate a provisioning pass already in flight for this host.
        with handler._remote_hosts_lock:
            handler._provisioning_hosts.add(str(host_id))

        dispatched = handler._maybe_dispatch_remote_gateway_provisioning(AgentId(), host_id, ssh_info, "imbue_cloud")

        # Coalesced: no second pass was dispatched, and the in-flight guard is
        # left intact for the pass that is already running.
        assert dispatched is False
        assert handler._provisioned == []
        with handler._remote_hosts_lock:
            assert handler._provisioning_hosts == {str(host_id)}


def test_provisioning_skips_host_already_provisioned_this_session(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    """A host already provisioned this supervisor lifetime is not re-provisioned.

    The discovery stream re-emits the full agent set every cycle; re-running the
    expensive idempotent provisioning each time is wasteful, so an
    already-provisioned host is skipped (a supervisor restart re-provisions).
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    tunnel_manager = _RecordingTunnelManager()
    host_id = HostId()
    ssh_info = RemoteSSHInfo(user="root", host="192.0.2.1", port=2222, key_path=tmp_path / "k")
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _ProvisionRecordingHandler(
            latchkey=manager,
            tunnel_manager=tunnel_manager,
            concurrency_group=cg,
            mngr_ctx=temp_mngr_ctx,
        )
        # Mark the host as already provisioned this session.
        with handler._remote_hosts_lock:
            handler._provisioned_hosts.add(str(host_id))

        dispatched = handler._maybe_dispatch_remote_gateway_provisioning(AgentId(), host_id, ssh_info, "imbue_cloud")

        # Skipped: no new pass dispatched and nothing marked in flight.
        assert dispatched is False
        assert handler._provisioned == []
        with handler._remote_hosts_lock:
            assert handler._provisioning_hosts == set()


class _SyncRecordingHandler(LatchkeyDiscoveryHandler):
    """Handler stub that records ``_sync_state_to_host`` calls instead of opening VPS connections."""

    _synced: list[tuple[str, bool, bool]] = PrivateAttr(default_factory=list)

    def _sync_state_to_host(
        self,
        host_id_str: str,
        provider_name: str,
        *,
        do_permissions: bool,
        do_credentials: bool,
    ) -> None:
        self._synced.append((host_id_str, do_permissions, do_credentials))


def _make_sync_recording_handler(
    tmp_path: Path, temp_mngr_ctx: MngrContext, cg: ConcurrencyGroup
) -> _SyncRecordingHandler:
    return _SyncRecordingHandler(
        latchkey=FakeLatchkey(latchkey_directory=tmp_path),
        tunnel_manager=SSHTunnelManager(),
        concurrency_group=cg,
        mngr_ctx=temp_mngr_ctx,
    )


def test_remote_state_sync_initial_pass_does_permissions_then_credentials_for_known_hosts(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host_id_str = str(HostId())
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _make_sync_recording_handler(tmp_path, temp_mngr_ctx, cg)
        with handler._remote_hosts_lock:
            handler._remote_host_provider_by_id[host_id_str] = "imbue_cloud"
        handler._sync_all_known_hosts()
        # Full initial sync requests both permissions and credentials for the host.
        assert handler._synced == [(host_id_str, True, True)]


def test_remote_state_watch_handler_routes_credential_and_permission_changes(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host_id = HostId()
    host_id_str = str(host_id)
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _make_sync_recording_handler(tmp_path, temp_mngr_ctx, cg)
        with handler._remote_hosts_lock:
            handler._remote_host_provider_by_id[host_id_str] = "imbue_cloud"

        credentials_path = local_credentials_path(tmp_path)
        permissions_path = permissions_path_for_host(handler.latchkey.plugin_data_dir, host_id)
        event_handler = _LatchkeyStateChangeHandler(
            credentials_path=credentials_path,
            plugin_data_dir=handler.latchkey.plugin_data_dir,
            known_remote_host_ids=handler._known_remote_host_ids,
            on_credentials_changed=handler._sync_credentials_to_all_known_hosts,
            on_host_permissions_changed=handler._sync_permissions_to_host,
        )

        # A change to the credentials file pushes credentials (only) to all hosts.
        event_handler.dispatch(FileModifiedEvent(str(credentials_path)))
        assert handler._synced == [(host_id_str, False, True)]

        # A change to a host's permissions file pushes permissions (only) to that host.
        handler._synced.clear()
        event_handler.dispatch(FileModifiedEvent(str(permissions_path)))
        assert handler._synced == [(host_id_str, True, False)]

        # An unrelated path (e.g. the forward supervisor record) is ignored.
        handler._synced.clear()
        event_handler.dispatch(FileModifiedEvent(str(tmp_path / "mngr_latchkey" / "latchkey_forward.json")))
        assert handler._synced == []

        # A permissions file for an unknown host is ignored.
        handler._synced.clear()
        unknown_permissions = permissions_path_for_host(handler.latchkey.plugin_data_dir, HostId())
        event_handler.dispatch(FileModifiedEvent(str(unknown_permissions)))
        assert handler._synced == []

        # Regression: the watchdog observer stores handlers in a set, so the
        # handler must be hashable and schedulable (a MutableModel would raise
        # ``TypeError: unhashable type`` here).
        assert hash(event_handler) is not None
        Observer().schedule(event_handler, str(tmp_path), recursive=False)


def test_remote_state_watch_sentinel_fails_loudly_when_observer_dies(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        handler = _make_sync_recording_handler(tmp_path, temp_mngr_ctx, cg)
        # A stopped observer that did NOT stop because of shutdown is a watcher
        # failure -- the sentinel must raise loudly.
        observer = Observer()
        observer.start()
        observer.stop()
        observer.join()
        shutdown_event = threading.Event()
        with pytest.raises(RemoteGatewayError, match="stopped unexpectedly"):
            handler._fail_loudly_if_observer_dies(observer, shutdown_event)

        # When the observer stops *because* of shutdown, that is expected -- no raise.
        shutdown_event.set()
        handler._fail_loudly_if_observer_dies(observer, shutdown_event)


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
        'if sys.argv[1] == "--version":\n'
        f"    print('{LATCHKEY_MIN_VERSION}')\n"
        "    sys.exit(0)\n"
        'if sys.argv[1] == "ensure-browser":\n'
        "    counter_path = os.environ['FAKE_LATCHKEY_COUNTER']\n"
        # Record the encryption key the child was spawned with so the test can
        # confirm it was injected (otherwise Latchkey would consult the keychain).
        "    open(counter_path, 'a').write(os.environ.get('LATCHKEY_ENCRYPTION_KEY', '') + '\\n')\n"
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
    # Clear any operator-set key so the per-directory key is the one injected.
    monkeypatch.delenv("LATCHKEY_ENCRYPTION_KEY", raising=False)
    fake_binary = _make_fake_latchkey_binary_with_ensure_browser_counter(tmp_path, counter_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        # Multiple start_gateway calls -- the gateway is shared.
        for _ in range(3):
            manager.start_gateway(cg)

        # ensure-browser must have run exactly once.
        assert _wait_for_counter(counter_path, expected=1) == 1
        # And a log file for ensure-browser got written in the minds data dir.
        assert ensure_browser_log_path(manager.plugin_data_dir).is_file()
        # The ensure-browser child must have been handed the per-directory
        # encryption key, so Latchkey never falls through to the system
        # keychain (which on macOS pops a keychain access dialog).
        ensure_browser_keys = counter_path.read_text().splitlines()
        assert ensure_browser_keys == [load_or_create_encryption_key(tmp_path).get_secret_value()]
        manager.stop_gateway()


def test_ensure_browser_not_called_when_binary_missing(tmp_path: Path) -> None:
    """If the binary is missing at spawn time, the manager must raise
    without trying to spawn ``ensure-browser`` (there's nothing to run).

    Initialize against a working fake first so we pass the version
    check, then remove the binary so the spawn-time check fires.
    """
    fake_binary = _make_fake_latchkey_binary(tmp_path)
    manager = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(fake_binary))
    manager.initialize()
    fake_binary.unlink()
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        with pytest.raises(LatchkeyBinaryNotFoundError):
            manager.start_gateway(cg)
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


def test_services_info_offline_passes_offline_flag(tmp_path: Path) -> None:
    report_path = tmp_path / "argv_report"
    script = tmp_path / "latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(report_path)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(json.dumps({'credentialStatus': 'valid'}))\n"
    )
    script.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(script))

    latchkey.services_info("slack", is_offline=True)
    assert json.loads(report_path.read_text()) == ["services", "info", "slack", "--offline"]

    # Without the flag, ``--offline`` is absent.
    latchkey.services_info("slack")
    assert json.loads(report_path.read_text()) == ["services", "info", "slack"]


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


def _make_prepare_required_binary(
    tmp_path: Path,
    *,
    prepare_exit_code: int = 0,
    prepare_stderr: str = "",
) -> Path:
    """Build a fake latchkey CLI that mimics the 'requires preparation first' workflow.

    ``auth browser <service>`` exits 1 with latchkey's actual error
    message until ``auth browser-prepare <service>`` has been run; the
    prepare step writes a sentinel file that subsequent ``auth browser``
    calls look for. ``prepare_exit_code`` / ``prepare_stderr`` let tests
    force the prepare step itself to fail.
    """
    script = tmp_path / "latchkey"
    report_path = tmp_path / "latchkey_report.jsonl"
    prepared_marker = tmp_path / "prepared_marker"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        f"with open({str(report_path)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"prepared_marker = {str(prepared_marker)!r}\n"
        "if argv[:2] == ['auth', 'browser-prepare']:\n"
        f"    if {prepare_exit_code} == 0:\n"
        "        open(prepared_marker, 'w').close()\n"
        f"    if {prepare_stderr!r}:\n"
        f"        sys.stderr.write({prepare_stderr!r})\n"
        f"    sys.exit({prepare_exit_code})\n"
        "if argv[:2] == ['auth', 'browser']:\n"
        "    if not os.path.exists(prepared_marker):\n"
        "        service = argv[2] if len(argv) > 2 else '<svc>'\n"
        "        sys.stderr.write(\n"
        "            'Error: Service ' + service + ' requires preparation first. '\n"
        '            "Run \'latchkey auth browser-prepare " + service + "\' before logging in.\\n"\n'
        "        )\n"
        "        sys.exit(1)\n"
        "    sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    return script


def _read_recording_report(tmp_path: Path) -> list[dict[str, object]]:
    report_path = tmp_path / "latchkey_report.jsonl"
    return [json.loads(line) for line in report_path.read_text().splitlines() if line.strip()]


def test_auth_browser_runs_browser_prepare_and_retries_when_preparation_required(tmp_path: Path) -> None:
    """Auto-recovery path: latchkey signals preparation-required, we prepare and retry."""
    binary = _make_prepare_required_binary(tmp_path)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("slack")

    assert is_success is True
    assert detail == ""
    records = _read_recording_report(tmp_path)
    argv_calls = [record["argv"] for record in records]
    assert argv_calls == [
        ["auth", "browser", "slack"],
        ["auth", "browser-prepare", "slack"],
        ["auth", "browser", "slack"],
    ]


def test_auth_browser_reports_failure_when_browser_prepare_fails(tmp_path: Path) -> None:
    """If the prepare step itself fails, surface that failure and do not retry the browser flow."""
    binary = _make_prepare_required_binary(
        tmp_path,
        prepare_exit_code=1,
        prepare_stderr="prepare blew up",
    )
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("slack")

    assert is_success is False
    assert detail == "prepare blew up"
    argv_calls = [record["argv"] for record in _read_recording_report(tmp_path)]
    assert argv_calls == [
        ["auth", "browser", "slack"],
        ["auth", "browser-prepare", "slack"],
    ]


def test_auth_browser_does_not_retry_on_unrelated_failure(tmp_path: Path) -> None:
    """A failure without the preparation-required marker is returned as-is, with no extra calls."""
    binary = _make_recording_binary(tmp_path, exit_code=1, stderr="user cancelled")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("slack")

    assert is_success is False
    assert detail == "user cancelled"
    argv_calls = [record["argv"] for record in _read_recording_report(tmp_path)]
    assert argv_calls == [["auth", "browser", "slack"]]


# -- auth_browser_login / auth_prepare / auth_clear --


def test_auth_browser_login_reports_success_on_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=0)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser_login("slack")

    assert is_success is True
    assert detail == ""
    argv_calls = [record["argv"] for record in _read_recording_report(tmp_path)]
    assert argv_calls == [["auth", "browser", "slack"]]


def test_auth_browser_login_does_not_run_browser_prepare_on_failure(tmp_path: Path) -> None:
    """Unlike ``auth_browser``, the bare login never auto-runs ``browser-prepare``."""
    binary = _make_prepare_required_binary(tmp_path)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser_login("slack")

    assert is_success is False
    assert "browser-prepare" in detail.lower()
    # Only the single bare ``auth browser`` call; no ``browser-prepare``, no retry.
    argv_calls = [record["argv"] for record in _read_recording_report(tmp_path)]
    assert argv_calls == [["auth", "browser", "slack"]]


def test_auth_prepare_invokes_prepare_with_json_payload(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=0)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_prepare("google-gmail", "client-id-123", "secret-xyz")

    assert is_success is True
    assert detail == ""
    records = _read_recording_report(tmp_path)
    assert len(records) == 1
    argv = records[0]["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["auth", "prepare", "google-gmail"]
    payload_arg = argv[3]
    assert isinstance(payload_arg, str)
    assert json.loads(payload_arg) == {"clientId": "client-id-123", "clientSecret": "secret-xyz"}


def test_auth_prepare_reports_failure_on_non_zero_exit(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=1, stderr="prepare failed")
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_prepare("google-gmail", "id", "secret")

    assert is_success is False
    assert detail == "prepare failed"


def test_minds_google_oauth_services_excludes_directions() -> None:
    # google-directions authenticates with an API key (latchkey ``set`` auth),
    # not OAuth, so it must never be routed through the Minds OAuth client.
    assert "google-directions" not in MINDS_GOOGLE_OAUTH_SERVICES
    assert "google-gmail" in MINDS_GOOGLE_OAUTH_SERVICES


def test_auth_clear_invokes_clear_with_yes_flag(tmp_path: Path) -> None:
    binary = _make_recording_binary(tmp_path, exit_code=0)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_clear("google-sheets")

    assert is_success is True
    assert detail == ""
    argv_calls = [record["argv"] for record in _read_recording_report(tmp_path)]
    assert argv_calls == [["auth", "clear", "-y", "google-sheets"]]


# -- auth_browser Minds Google OAuth client preference --


def _make_google_oauth_binary(
    tmp_path: Path,
    *,
    is_client_preregistered: bool = False,
    does_minds_prepare_succeed: bool = True,
    does_minds_login_succeed: bool = True,
    does_preregistered_login_succeed: bool = True,
    does_self_setup_prepare_succeed: bool = True,
) -> Path:
    """Build a fake latchkey CLI that models the google OAuth client lifecycle.

    A marker file records which client is registered: ``auth prepare`` writes
    ``minds``, ``auth browser-prepare`` writes ``self-setup``, ``auth clear``
    removes it, and an optional pre-existing client starts as ``preregistered``.
    ``auth browser`` fails asking for ``browser-prepare`` when nothing is
    registered, and otherwise succeeds or fails per the registered client's
    configured outcome. Every invocation appends its argv to the shared
    recording report.
    """
    script = tmp_path / "latchkey"
    report_path = tmp_path / "latchkey_report.jsonl"
    marker_path = tmp_path / "client_marker"
    if is_client_preregistered:
        marker_path.write_text("preregistered")
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        f"report_path = {str(report_path)!r}\n"
        f"marker_path = {str(marker_path)!r}\n"
        "with open(report_path, 'a') as handle:\n"
        "    handle.write(json.dumps({'argv': argv}) + '\\n')\n"
        "marker = open(marker_path).read() if os.path.exists(marker_path) else ''\n"
        "if argv[:2] == ['auth', 'prepare']:\n"
        f"    if {does_minds_prepare_succeed}:\n"
        "        open(marker_path, 'w').write('minds')\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('minds prepare failed')\n"
        "    sys.exit(1)\n"
        "if argv[:2] == ['auth', 'browser-prepare']:\n"
        f"    if {does_self_setup_prepare_succeed}:\n"
        "        open(marker_path, 'w').write('self-setup')\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('self-setup prepare failed')\n"
        "    sys.exit(1)\n"
        "if argv[:2] == ['auth', 'clear']:\n"
        "    if os.path.exists(marker_path):\n"
        "        os.remove(marker_path)\n"
        "    sys.exit(0)\n"
        "if argv[:2] == ['auth', 'browser']:\n"
        "    service = argv[2] if len(argv) > 2 else '<svc>'\n"
        "    if marker == '':\n"
        "        sys.stderr.write(\n"
        "            'Error: Service ' + service + ' requires preparation first. '\n"
        '            "Run \'latchkey auth browser-prepare " + service + "\' before logging in.\\n"\n'
        "        )\n"
        "        sys.exit(1)\n"
        f"    if marker == 'minds' and not {does_minds_login_succeed}:\n"
        "        sys.stderr.write('minds consent declined')\n"
        "        sys.exit(1)\n"
        f"    if marker == 'preregistered' and not {does_preregistered_login_succeed}:\n"
        "        sys.stderr.write('token expired')\n"
        "        sys.exit(1)\n"
        "    sys.exit(0)\n"
        "sys.exit(2)\n"
    )
    script.chmod(0o755)
    return script


def _read_argv_calls(tmp_path: Path) -> list[object]:
    return [record["argv"] for record in _read_recording_report(tmp_path)]


# The exact ``auth prepare`` invocation we expect for the Minds-provided client.
_MINDS_PREPARE_ARGV = [
    "auth",
    "prepare",
    "google-gmail",
    json.dumps({"clientId": MINDS_GOOGLE_OAUTH_CLIENT_ID, "clientSecret": MINDS_GOOGLE_OAUTH_CLIENT_SECRET}),
]


def test_auth_browser_google_registers_minds_client_then_signs_in(tmp_path: Path) -> None:
    """No client registered: register the Minds client and sign in; no clear, no self-setup."""
    binary = _make_google_oauth_binary(tmp_path)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("google-gmail")

    assert is_success is True
    assert detail == ""
    assert _read_argv_calls(tmp_path) == [
        ["auth", "browser", "google-gmail"],
        _MINDS_PREPARE_ARGV,
        ["auth", "browser", "google-gmail"],
    ]


def test_auth_browser_google_minds_sign_in_failure_clears_then_self_setup(tmp_path: Path) -> None:
    """Minds client registers but its sign-in fails: clear it, then the self-setup flow succeeds."""
    binary = _make_google_oauth_binary(tmp_path, does_minds_login_succeed=False)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, _detail = latchkey.auth_browser("google-gmail")

    assert is_success is True
    # The clear sits between the failed Minds sign-in and the self-setup
    # browser-prepare, so the self-setup flow starts from a clean slate.
    assert _read_argv_calls(tmp_path) == [
        ["auth", "browser", "google-gmail"],
        _MINDS_PREPARE_ARGV,
        ["auth", "browser", "google-gmail"],
        ["auth", "clear", "-y", "google-gmail"],
        ["auth", "browser-prepare", "google-gmail"],
        ["auth", "browser", "google-gmail"],
    ]


def test_auth_browser_google_minds_prepare_failure_falls_through_without_clearing(tmp_path: Path) -> None:
    """If registering the Minds client fails, skip the sign-in and the clear and go to self-setup."""
    binary = _make_google_oauth_binary(tmp_path, does_minds_prepare_succeed=False)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, _detail = latchkey.auth_browser("google-gmail")

    assert is_success is True
    argv_calls = _read_argv_calls(tmp_path)
    assert argv_calls == [
        ["auth", "browser", "google-gmail"],
        _MINDS_PREPARE_ARGV,
        ["auth", "browser-prepare", "google-gmail"],
        ["auth", "browser", "google-gmail"],
    ]
    # We never registered our client, so nothing of ours is cleared.
    assert ["auth", "clear", "-y", "google-gmail"] not in argv_calls


def test_auth_browser_google_already_registered_signs_in_with_one_call(tmp_path: Path) -> None:
    """A pre-existing working client signs in with a single call: no prepare, no clear."""
    binary = _make_google_oauth_binary(tmp_path, is_client_preregistered=True)
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("google-gmail")

    assert is_success is True
    assert detail == ""
    assert _read_argv_calls(tmp_path) == [["auth", "browser", "google-gmail"]]


def test_auth_browser_google_existing_client_failure_is_never_cleared(tmp_path: Path) -> None:
    """A registered client that is not ours, whose sign-in fails, is returned as-is and never cleared."""
    binary = _make_google_oauth_binary(
        tmp_path,
        is_client_preregistered=True,
        does_preregistered_login_succeed=False,
    )
    latchkey = Latchkey(latchkey_directory=tmp_path, latchkey_binary=str(binary))

    is_success, detail = latchkey.auth_browser("google-gmail")

    assert is_success is False
    assert detail == "token expired"
    argv_calls = _read_argv_calls(tmp_path)
    # The pre-existing client is preserved: no prepare and no clear, because we
    # only ever touch a client we registered ourselves.
    assert argv_calls == [["auth", "browser", "google-gmail"]]
    assert ["auth", "clear", "-y", "google-gmail"] not in argv_calls
