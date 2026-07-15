"""End-to-end release test for the minds Latchkey remote-workspace auth flow.

Exercises the full ``mngr_latchkey`` remote-workspace lifecycle that minds
drives for VPS-backed workspaces -- against a **fake VPS** that is really this
very machine reached as ``root`` over SSH, so the test needs no cloud
credentials at all:

1. A throwaway root sshd is started on a random loopback port (via
   ``sudo``), and a ``[providers.docker]`` block pointing at
   ``ssh://root@127.0.0.1:<port>`` makes mngr treat this machine's own Docker
   daemon as a *remote* outer host. Latchkey's remote-vs-local decision is
   purely connector-based (``OuterHost.is_local``), so every genuinely-remote
   code path runs: VPS gateway provisioning, the VPS->container reverse
   tunnel, and the credential/permission remote-state sync.
2. A workspace (Docker host + agent) is created through the same CLI flow
   minds' agent creator uses: ``mngr latchkey create-agent-env`` ->
   ``mngr create --host-env ...`` -> ``mngr latchkey link-permissions``.
3. ``mngr latchkey forward`` (the supervisor minds spawns) discovers the
   agent, reverse-tunnels the desktop gateway into the workspace, provisions
   the VPS-resident secondary gateway, and starts the remote-state watcher.

Asserted end-to-end, from *inside* the workspace via ``mngr exec``:

a. The desktop ("local") latchkey gateway is reachable on
   ``$LATCHKEY_GATEWAY`` (``127.0.0.1:1989``): ``GET /permissions/self`` with
   the injected password + permissions-override JWT returns the agent's
   baseline permissions.
b. The secondary (VPS-resident) gateway is reachable on
   ``$LATCHKEY_GATEWAY_SECONDARY`` (``127.0.0.1:1990``) and its listen
   password is wired to the same desktop-derived value (a wrong password is
   answered differently from the right one). ``/permissions/self`` is not
   asserted here: it is served by the bundled ``permissions.mjs`` extension,
   which is only materialized for the desktop gateway -- the VPS gateway runs
   the bare upstream ``latchkey gateway``.
c. Updating the workspace's local ``latchkey_permissions.json`` (granting the
   ``slack-api`` scope, with slack credentials pre-seeded in the local store)
   makes the remote-state watcher push both the permissions
   (``/root/.latchkey/permissions.json``) *and* the filtered credential
   bundle (``/root/.latchkey/credentials.json.enc``) onto the VPS outer host
   automatically.

Gating: this test is deliberately invasive on the machine that runs it (it
needs passwordless ``sudo`` to run a root sshd, apt-installs ``supervisor``
on the "VPS" = this machine, npm-installs the latchkey CLI globally as root,
and writes under ``/root/.latchkey`` + ``/etc/supervisor/conf.d/``), so it is
opt-in via ``MNGR_LATCHKEY_E2E_TESTS=1``. CI sets the variable in the
``test-minds-release`` job (the ``run_minds_release_tests`` manual dispatch),
which runs on a throwaway GitHub ubuntu runner. Once opted in, missing
prerequisites are hard *failures*, not skips, so a broken CI environment can
never silently hollow the test out.

Run manually (on a disposable Linux machine!):

    MNGR_LATCHKEY_E2E_TESTS=1 just test apps/minds/test_latchkey_e2e.py
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import build_test_known_hosts_file
from imbue.mngr.utils.testing import find_free_port
from imbue.mngr.utils.testing import generate_ssh_keypair
from imbue.mngr.utils.testing import is_port_open
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PASSWORD
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
from imbue.mngr_latchkey.agent_setup import ENV_LATCHKEY_GATEWAY_SECONDARY
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.encryption_key import encryption_key_path
from imbue.mngr_latchkey.remote_gateway import INNER_PORT
from imbue.mngr_latchkey.remote_gateway import LATCHKEY_VERSION
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.store import save_permissions

# Opt-in gate; see the module docstring for why this is not enabled by default.
_OPT_IN_ENV_VAR: Final[str] = "MNGR_LATCHKEY_E2E_TESTS"

# NOTE: no ``rsync`` mark. The source checkout handed to ``mngr create`` is a
# *git repo*, so the transfer resolves to GIT_MIRROR (git push), not rsync --
# the same reasoning (verified empirically there) as
# ``test_aws_workspace_release.py``. The rsync resource-guard fails tests that
# carry the mark without invoking rsync.
pytestmark = [
    pytest.mark.release,
    pytest.mark.docker,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get(_OPT_IN_ENV_VAR) != "1",
        reason=f"{_OPT_IN_ENV_VAR}=1 not set (this test runs a root sshd and installs packages on this machine)",
    ),
]

# The fake VPS is this machine's loopback, reached as root over the throwaway sshd.
_VPS_SSH_HOST: Final[str] = "127.0.0.1"

# Paths the remote-gateway provisioning writes on the "VPS" (= this machine,
# as root). Mirrors mngr_latchkey.remote_gateway's remote layout: the remote
# LATCHKEY_DIRECTORY is ``$HOME/.latchkey`` for the root ssh user.
_VPS_LATCHKEY_DIR: Final[str] = "/root/.latchkey"
_VPS_PERMISSIONS_PATH: Final[str] = f"{_VPS_LATCHKEY_DIR}/permissions.json"
_VPS_CREDENTIALS_PATH: Final[str] = f"{_VPS_LATCHKEY_DIR}/credentials.json.enc"
_VPS_SUPERVISOR_CONF_GLOB: Final[str] = "/etc/supervisor/conf.d/latchkey-*.conf"

# Latchkey scope granted in step (c). Must be a scope the bundled
# services.json catalog maps back to the ``slack`` service, so the
# credential sync ships the seeded slack credentials.
_GRANTED_SCOPE: Final[str] = "slack-api"
_GRANTED_SERVICE: Final[str] = "slack"

# Subprocess budgets. The docker host-image build (debian-slim + a handful of
# apt packages) plus container start dominates the create; VPS provisioning
# (apt-get install supervisor + npm install -g latchkey, as root over ssh)
# dominates the forward-driven phase.
_NPM_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0
_CREATE_TIMEOUT_SECONDS: Final[int] = 900
_QUICK_MNGR_TIMEOUT_SECONDS: Final[int] = 120
_DESKTOP_GATEWAY_REACHABLE_TIMEOUT_SECONDS: Final[float] = 300.0
_VPS_GATEWAY_REACHABLE_TIMEOUT_SECONDS: Final[float] = 480.0
_SYNC_CONVERGENCE_TIMEOUT_SECONDS: Final[float] = 180.0
_POLL_INTERVAL_SECONDS: Final[float] = 3.0

# Marker prefixing the HTTP status code in the secondary-gateway probes, so
# the status is extractable from ``mngr exec`` output even if mngr adds its
# own lines around the remote command's stdout.
_HTTP_STATUS_MARKER: Final[str] = "LK_E2E_HTTP_STATUS:"
_HTTP_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(re.escape(_HTTP_STATUS_MARKER) + r"(\d{3})")


def _require(condition: bool, message: str) -> None:
    """Fail (not skip) when an opted-in run is missing a prerequisite.

    The opt-in env var is the only skip condition; everything after it must
    hold, otherwise a misconfigured CI runner would silently skip the test.
    """
    if not condition:
        pytest.fail(f"{_OPT_IN_ENV_VAR}=1 is set but a prerequisite is missing: {message}")


def _find_sshd_binary() -> Path | None:
    """Locate the sshd binary (PATH first, then the standard Debian location)."""
    which_result = shutil.which("sshd")
    if which_result is not None:
        return Path(which_result)
    debian_sshd = Path("/usr/sbin/sshd")
    return debian_sshd if debian_sshd.is_file() else None


def _sftp_server_path() -> Path:
    """Platform-appropriate sftp-server path (the outer-host SFTP writes need it)."""
    macos_path = Path("/usr/libexec/sftp-server")
    return macos_path if macos_path.is_file() else Path("/usr/lib/openssh/sftp-server")


def _install_latchkey_cli(prefix_dir: Path) -> Path:
    """npm-install the pinned upstream latchkey CLI into ``prefix_dir`` and return its binary path.

    Uses the same version pin the VPS provisioning installs remotely
    (:data:`LATCHKEY_VERSION`), so both gateways run identical latchkey builds.
    """
    prefix_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(prefix_dir),
            "--no-fund",
            "--no-audit",
            "--loglevel=error",
            f"latchkey@{LATCHKEY_VERSION}",
        ],
        capture_output=True,
        text=True,
        timeout=_NPM_INSTALL_TIMEOUT_SECONDS,
    )
    _require(result.returncode == 0, f"npm install latchkey@{LATCHKEY_VERSION} failed:\n{result.stderr}")
    binary = prefix_dir / "node_modules" / ".bin" / "latchkey"
    _require(binary.is_file(), f"latchkey binary missing after npm install at {binary}")
    return binary


@contextmanager
def _root_sshd(scratch: Path, authorized_public_key: str) -> Iterator[tuple[int, Path]]:
    """Run a throwaway root sshd on a random loopback port; yield ``(port, host_key_path)``.

    The daemon runs as root (via ``sudo``) so the SSH login lands as ``root``
    -- the user the remote-gateway provisioning requires (it apt-installs
    packages, writes supervisord drop-ins, and uses a root-owned tmpfs
    secrets dir). ``StrictModes no`` lets sshd accept the test-user-owned
    key/config files without any chown dance. ``PidFile none`` keeps sshd
    from dropping a root-owned file into the pytest tmp tree (which would
    break tmp_path cleanup).
    """
    sshd_binary = _find_sshd_binary()
    _require(sshd_binary is not None, "sshd not found (install openssh-server)")
    # Narrow for the type checker; _require already failed the test when None.
    assert sshd_binary is not None

    sshd_dir = scratch / "root-sshd"
    sshd_dir.mkdir()
    host_key_path = sshd_dir / "ssh_host_ed25519_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(host_key_path), "-N", "", "-q"],
        check=True,
        timeout=30,
    )
    authorized_keys_path = sshd_dir / "authorized_keys"
    authorized_keys_path.write_text(authorized_public_key)

    port = find_free_port()
    sshd_config_path = sshd_dir / "sshd_config"
    sshd_config_path.write_text(
        f"""
Port {port}
ListenAddress {_VPS_SSH_HOST}
HostKey {host_key_path}
AuthorizedKeysFile {authorized_keys_path}
PermitRootLogin prohibit-password
PasswordAuthentication no
ChallengeResponseAuthentication no
UsePAM no
StrictModes no
PidFile none
AllowUsers root
Subsystem sftp {_sftp_server_path()}
"""
    )

    # sshd's privilege-separation directory must exist before it will start.
    subprocess.run(["sudo", "-n", "mkdir", "-p", "/run/sshd"], check=True, timeout=30)
    process = subprocess.Popen(
        ["sudo", "-n", str(sshd_binary), "-D", "-e", "-f", str(sshd_config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        is_up = poll_until(lambda: is_port_open(port), timeout=30.0, poll_interval=0.2)
        _require(is_up, f"root sshd failed to start listening on {_VPS_SSH_HOST}:{port}")
        yield port, host_key_path
    finally:
        # sudo forwards SIGTERM to the sshd it runs; fall back to a hard kill.
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _write_ssh_client_setup(home: Path, private_key: Path, host_key_path: Path, port: int) -> Path:
    """Materialize the SSH client config for the fake VPS; return the config path.

    Written into the isolated ``home``'s ``.ssh/`` so paramiko-based consumers
    (pyinfra's outer-host connector, docker-py's ssh transport -- both resolve
    ``~`` via ``$HOME``) pick up the key, the config, and the pre-populated
    known_hosts. The OpenSSH *binary* ignores ``$HOME`` (it resolves ``~``
    from the passwd entry), so CLI consumers -- notably the ``docker`` CLI's
    ssh transport for ``DOCKER_HOST=ssh://...`` -- are covered separately by
    the PATH shim from :func:`_write_ssh_binary_shim`, which forces this
    config file via ``ssh -F``.
    """
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    identity_path = ssh_dir / "id_ed25519"
    identity_path.write_bytes(private_key.read_bytes())
    identity_path.chmod(0o600)
    known_hosts_path = build_test_known_hosts_file(host_key_path, port, ssh_dir / "known_hosts")
    config_path = ssh_dir / "config"
    # Scoped to the fake-VPS loopback host. mngr's own ssh invocations against
    # *containers* also target 127.0.0.1 but pass explicit -p/-i/-o flags,
    # which take precedence over everything configured here.
    config_path.write_text(
        f"""
Host {_VPS_SSH_HOST}
  User root
  Port {port}
  IdentityFile {identity_path}
  UserKnownHostsFile {known_hosts_path}
  StrictHostKeyChecking accept-new
"""
    )
    config_path.chmod(0o600)
    return config_path


def _write_ssh_binary_shim(shim_dir: Path, ssh_config_path: Path) -> None:
    """Drop an ``ssh`` shim into ``shim_dir`` that forces our client config via ``-F``.

    The OpenSSH binary resolves its default config from the passwd home, not
    ``$HOME``, so the isolated-home config alone never reaches CLI consumers.
    Prepending this shim to ``PATH`` covers them (the ``docker`` CLI's
    ``ssh://`` transport, git-over-ssh pushes into the container, rsync's ssh
    transport) without touching the real user's ``~/.ssh``.
    """
    real_ssh = shutil.which("ssh")
    _require(real_ssh is not None, "ssh client binary not found")
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "ssh"
    shim_path.write_text(f'#!/bin/sh\nexec {shlex.quote(str(real_ssh))} -F {shlex.quote(str(ssh_config_path))} "$@"\n')
    shim_path.chmod(0o755)


def _make_temp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo used as ``mngr create``'s source checkout (cwd)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("latchkey e2e release test source\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    return repo


def _build_release_env(
    tmp_path: Path, home: Path, shim_dir: Path, vps_ssh_port: int, latchkey_binary: Path
) -> dict[str, str]:
    """Build the subprocess env + opted-in mngr config for the release test.

    Writes a self-contained project ``settings.toml`` (pointed at via
    ``MNGR_PROJECT_CONFIG_DIR``) whose docker provider targets the fake VPS's
    daemon over SSH -- the one configuration that makes the workspace's outer
    host *remote* from latchkey's perspective. ``MNGR_HOST_DIR`` and ``HOME``
    are isolated to tmp so no developer mngr profile / config is loaded.
    """
    settings_dir = tmp_path / "config"
    settings_dir.mkdir()
    settings_dir.joinpath("settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        "\n[providers.docker]\n"
        'backend = "docker"\n'
        f'host = "ssh://root@{_VPS_SSH_HOST}:{vps_ssh_port}"\n'
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )
    env = dict(os.environ)
    env["MNGR_PROJECT_CONFIG_DIR"] = str(settings_dir)
    env["MNGR_HOST_DIR"] = str(tmp_path / "mngr_home")
    env["HOME"] = str(home)
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir(parents=True, exist_ok=True)
    env["MNGR_LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    env["MNGR_LATCHKEY_BINARY"] = str(latchkey_binary)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    return env


def _run_mngr(
    env: dict[str, str], cwd: Path, *args: str, timeout: int = _QUICK_MNGR_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run the monorepo's ``mngr`` (the dev shim / venv entry point on PATH).

    stdout and stderr are streamed to separate files (outside ``cwd``, so the
    source git repo stays clean for ``mngr create``) and returned in the
    CompletedProcess, so JSON/JSONL stdout stays parseable and a stuck command
    is still diagnosable on timeout.
    """
    command_label = args[0] if args else "cmd"
    stdout_path = cwd.parent / f"mngr-{command_label}-{uuid.uuid4().hex}.out"
    stderr_path = stdout_path.with_suffix(".err")
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file:
        process = subprocess.Popen(
            ["mngr", *args],
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            cwd=str(cwd),
            env=env,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            returncode = 124
    return subprocess.CompletedProcess(
        args=list(args), returncode=returncode, stdout=stdout_path.read_text(), stderr=stderr_path.read_text()
    )


def _run_on_vps(ssh_config_path: Path, command: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Execute ``command`` on the fake VPS (this machine, as root over the throwaway sshd)."""
    return subprocess.run(
        ["ssh", "-F", str(ssh_config_path), f"root@{_VPS_SSH_HOST}", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _parse_created_event(create_stdout: str) -> tuple[str, str]:
    """Extract ``(agent_id, host_id)`` from ``mngr create --format jsonl`` output."""
    for line in create_stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = event.get("agent_id")
            host_id = event.get("host_id")
            assert isinstance(agent_id, str) and agent_id, f"created event missing agent_id: {event}"
            assert isinstance(host_id, str) and host_id, f"created event missing host_id: {event}"
            return agent_id, host_id
    pytest.fail(f"no 'created' event found in mngr create output:\n{create_stdout}")


def _seed_local_slack_credentials(latchkey_binary: Path, latchkey_directory: Path) -> None:
    """Store fake slack credentials in the *local* latchkey credential store.

    Uses ``latchkey auth set`` with an explicit ``LATCHKEY_ENCRYPTION_KEY``
    (read from the per-directory key file, the same way minds' packaged e2e
    harness seeds credentials) so the store is encrypted with the key the
    remote sync later re-encrypts from.
    """
    key_file = encryption_key_path(latchkey_directory)
    assert key_file.is_file(), f"latchkey encryption key missing at {key_file} (create-agent-env should create it)"
    env = dict(os.environ)
    env["LATCHKEY_DIRECTORY"] = str(latchkey_directory)
    env["LATCHKEY_ENCRYPTION_KEY"] = key_file.read_text().strip()
    result = subprocess.run(
        [
            str(latchkey_binary),
            "auth",
            "set",
            _GRANTED_SERVICE,
            "-H",
            f"Authorization: Bearer xoxc-latchkey-e2e-fake-{uuid.uuid4().hex}",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"latchkey auth set {_GRANTED_SERVICE} failed:\n{result.stderr}\n{result.stdout}"


def _grant_scope_in_host_permissions(latchkey_directory: Path, host_id: str) -> Path:
    """Append an ``{_GRANTED_SCOPE}: [any]`` rule to the host's canonical permissions file.

    Uses the plugin's own load/save helpers so the write is atomic
    (tmp + rename), exactly like the desktop client's permission-grant flow --
    which is also the write shape the remote-state watcher listens for.
    Returns the canonical permissions path.
    """
    permissions_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), HostId(host_id))
    assert permissions_path.is_file(), (
        f"canonical host permissions file missing at {permissions_path}; link-permissions should have created it"
    )
    config = load_permissions(permissions_path)
    updated = LatchkeyPermissionsConfig(
        rules=config.rules + ({_GRANTED_SCOPE: ["any"]},),
        schemas=config.schemas,
    )
    save_permissions(permissions_path, updated)
    return permissions_path


def _exec_in_workspace(
    env: dict[str, str], cwd: Path, agent_address: str, command: str
) -> subprocess.CompletedProcess[str]:
    """Run a shell command inside the workspace container via ``mngr exec``."""
    return _run_mngr(env, cwd, "exec", agent_address, command, timeout=_QUICK_MNGR_TIMEOUT_SECONDS)


def _poll_workspace_probe(
    env: dict[str, str],
    cwd: Path,
    agent_address: str,
    command: str,
    is_success: Callable[[subprocess.CompletedProcess[str]], bool],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Re-run an in-workspace probe until ``is_success`` or ``timeout``; return the last result."""
    last_result: list[subprocess.CompletedProcess[str]] = []

    def _attempt() -> bool:
        result = _exec_in_workspace(env, cwd, agent_address, command)
        last_result.append(result)
        return is_success(result)

    poll_until(_attempt, timeout=timeout, poll_interval=_POLL_INTERVAL_SECONDS)
    assert last_result, "probe never ran"
    return last_result[-1]


def _curl_gateway_command(port: int, headers: dict[str, str], request_path: str) -> str:
    """Build an in-container curl command against a loopback gateway port.

    ``-f`` makes 4xx/5xx exit non-zero so pollers can key off the exit code;
    the body is printed on success.
    """
    header_args = " ".join(f"-H {shlex.quote(f'{name}: {value}')}" for name, value in headers.items())
    return f"curl -fsS -m 10 {header_args} http://127.0.0.1:{port}{request_path}"


def test_latchkey_remote_workspace_gateways_and_state_sync_end_to_end(tmp_path: Path) -> None:
    """Remote workspace reaches both latchkey gateways; local permission edits auto-sync to the VPS."""
    # -- Prerequisites (hard failures once opted in; see _require) ----------
    _require(shutil.which("docker") is not None, "docker CLI not found")
    docker_probe = subprocess.run(["docker", "version"], capture_output=True, text=True, timeout=60)
    _require(docker_probe.returncode == 0, f"docker daemon not reachable:\n{docker_probe.stderr}")
    sudo_probe = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True, timeout=30)
    _require(sudo_probe.returncode == 0, "passwordless sudo unavailable (needed to run the root sshd)")
    _require(shutil.which("npm") is not None, "npm not found (needed to install the latchkey CLI)")

    latchkey_binary = _install_latchkey_cli(tmp_path / "latchkey-cli")
    private_key, public_key = generate_ssh_keypair(tmp_path)

    with _root_sshd(tmp_path, public_key.read_text()) as (vps_ssh_port, host_key_path):
        home = tmp_path / "home"
        home.mkdir()
        ssh_config_path = _write_ssh_client_setup(home, private_key, host_key_path, vps_ssh_port)
        shim_dir = tmp_path / "shim-bin"
        _write_ssh_binary_shim(shim_dir, ssh_config_path)
        env = _build_release_env(tmp_path, home, shim_dir, vps_ssh_port, latchkey_binary)
        latchkey_directory = Path(env["MNGR_LATCHKEY_DIRECTORY"])
        repo = _make_temp_git_repo(tmp_path)
        latchkey_flags = ("--latchkey-directory", str(latchkey_directory), "--latchkey-binary", str(latchkey_binary))

        # Sanity: the fake VPS is reachable as root before anything heavier runs.
        vps_probe = _run_on_vps(ssh_config_path, "id -u && echo vps-ok")
        _require(vps_probe.returncode == 0, f"cannot ssh into the fake VPS as root:\n{vps_probe.stderr}")
        assert vps_probe.stdout.splitlines()[0].strip() == "0", f"VPS ssh login is not root:\n{vps_probe.stdout}"

        forward_process: subprocess.Popen[bytes] | None = None
        agent_address: str | None = None
        try:
            # -- Step 1: latchkey env for the new workspace (minds' create flow) --
            agent_env_result = _run_mngr(env, repo, "latchkey", "create-agent-env", *latchkey_flags)
            assert agent_env_result.returncode == 0, f"create-agent-env failed:\n{agent_env_result.stderr}"
            agent_env_payload = json.loads(agent_env_result.stdout.strip().splitlines()[-1])
            latchkey_env: dict[str, str] = agent_env_payload["env"]
            opaque_path = agent_env_payload["opaque_permissions_path"]
            assert latchkey_env[ENV_LATCHKEY_GATEWAY] == f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}"
            assert latchkey_env[ENV_LATCHKEY_GATEWAY_SECONDARY] == f"http://127.0.0.1:{INNER_PORT}"

            # Seed slack credentials into the local store *before* any grant,
            # so step (c)'s permission change is what ships them to the VPS.
            _seed_local_slack_credentials(latchkey_binary, latchkey_directory)

            # -- Step 2: create the remote workspace with the latchkey env --
            host_name = f"lk-e2e-{uuid.uuid4().hex}"
            agent_address = f"svc@{host_name}.docker"
            host_env_args: list[str] = []
            for key, value in latchkey_env.items():
                host_env_args.extend(["--host-env", f"{key}={value}"])
            create_result = _run_mngr(
                env,
                repo,
                "create",
                agent_address,
                "--new-host",
                "--type",
                "command",
                "--no-connect",
                "--format",
                "jsonl",
                *host_env_args,
                "--",
                "sleep",
                "86413",
                timeout=_CREATE_TIMEOUT_SECONDS,
            )
            assert create_result.returncode == 0, (
                f"mngr create failed:\nstdout:\n{create_result.stdout}\nstderr:\n{create_result.stderr}"
            )
            _agent_id, host_id = _parse_created_event(create_result.stdout)

            # -- Step 3: swing the opaque permissions handle to the canonical host path --
            link_result = _run_mngr(
                env,
                repo,
                "latchkey",
                "link-permissions",
                "--host-id",
                host_id,
                "--opaque-path",
                opaque_path,
                *latchkey_flags,
            )
            assert link_result.returncode == 0, f"link-permissions failed:\n{link_result.stderr}"

            # The workspace really carries the latchkey wiring in its host env.
            env_probe = _exec_in_workspace(
                env, repo, agent_address, f"printenv {ENV_LATCHKEY_GATEWAY} {ENV_LATCHKEY_GATEWAY_SECONDARY}"
            )
            assert env_probe.returncode == 0, f"printenv probe failed:\n{env_probe.stderr}"
            assert f"http://127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}" in env_probe.stdout
            assert f"http://127.0.0.1:{INNER_PORT}" in env_probe.stdout

            # -- Step 4: run the forward supervisor (gateway + discovery + provisioning + sync) --
            forward_log_path = tmp_path / "latchkey-forward.log"
            with forward_log_path.open("wb") as forward_log:
                forward_process = subprocess.Popen(
                    ["mngr", "latchkey", "forward", *latchkey_flags],
                    stdout=forward_log,
                    stderr=subprocess.STDOUT,
                    cwd=str(repo),
                    env=env,
                )

            password = latchkey_env[ENV_LATCHKEY_GATEWAY_PASSWORD]
            override_jwt = latchkey_env[ENV_LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE]

            # -- (a) the desktop gateway is reachable from inside the workspace --
            desktop_self_command = _curl_gateway_command(
                AGENT_SIDE_LATCHKEY_PORT,
                {
                    "X-Latchkey-Gateway-Password": password,
                    "X-Latchkey-Gateway-Permissions-Override": override_jwt,
                },
                "/permissions/self",
            )
            desktop_self = _poll_workspace_probe(
                env,
                repo,
                agent_address,
                desktop_self_command,
                is_success=lambda result: result.returncode == 0,
                timeout=_DESKTOP_GATEWAY_REACHABLE_TIMEOUT_SECONDS,
            )
            assert desktop_self.returncode == 0, (
                f"desktop gateway /permissions/self never succeeded from the workspace:\n"
                f"stdout:\n{desktop_self.stdout}\nstderr:\n{desktop_self.stderr}\n"
                f"forward log:\n{forward_log_path.read_text()}"
            )
            # The agent's own permissions view is the canonical host file: the
            # baseline carries the ``latchkey-self`` scope rule (the grant that
            # allowed this very /permissions/self read).
            assert "latchkey-self" in desktop_self.stdout, (
                f"unexpected /permissions/self body from the desktop gateway:\n{desktop_self.stdout}"
            )

            # -- (b) the secondary (VPS-resident) gateway is reachable from inside the workspace --
            # The VPS gateway runs the bare upstream latchkey (no bundled
            # extensions), so probe the gateway root rather than an extension
            # endpoint: with the correct password curl must receive an HTTP
            # response through the reverse tunnel.
            vps_gateway_probe_command = (
                f"curl -sS -m 10 -o /dev/null -w '{_HTTP_STATUS_MARKER}%{{http_code}}' "
                f"-H {shlex.quote(f'X-Latchkey-Gateway-Password: {password}')} "
                f"http://127.0.0.1:{INNER_PORT}/"
            )
            vps_gateway_probe = _poll_workspace_probe(
                env,
                repo,
                agent_address,
                vps_gateway_probe_command,
                is_success=lambda result: result.returncode == 0
                and _HTTP_STATUS_PATTERN.search(result.stdout) is not None,
                timeout=_VPS_GATEWAY_REACHABLE_TIMEOUT_SECONDS,
            )
            good_password_status_match = _HTTP_STATUS_PATTERN.search(vps_gateway_probe.stdout)
            assert good_password_status_match is not None, (
                f"secondary gateway never became reachable on 127.0.0.1:{INNER_PORT} inside the workspace:\n"
                f"stdout:\n{vps_gateway_probe.stdout}\nstderr:\n{vps_gateway_probe.stderr}\n"
                f"forward log:\n{forward_log_path.read_text()}"
            )
            # The listener is the latchkey gateway wired with the desktop-derived
            # password: a wrong password must be answered differently.
            wrong_password_command = (
                f"curl -sS -m 10 -o /dev/null -w '{_HTTP_STATUS_MARKER}%{{http_code}}' "
                f"-H {shlex.quote('X-Latchkey-Gateway-Password: definitely-wrong-95173')} "
                f"http://127.0.0.1:{INNER_PORT}/"
            )
            wrong_password_probe = _exec_in_workspace(env, repo, agent_address, wrong_password_command)
            wrong_password_status_match = _HTTP_STATUS_PATTERN.search(wrong_password_probe.stdout)
            assert wrong_password_status_match is not None, (
                f"wrong-password probe got no HTTP response:\n"
                f"stdout:\n{wrong_password_probe.stdout}\nstderr:\n{wrong_password_probe.stderr}"
            )
            assert wrong_password_status_match.group(1) != good_password_status_match.group(1), (
                "secondary gateway answered identical status codes for right and wrong gateway passwords "
                f"({good_password_status_match.group(1)}); it is not enforcing the shared password"
            )

            # -- (c) local permissions.json edits auto-sync files onto the VPS outer host --
            # Precondition: initial provisioning synced the deny-all baseline
            # permissions and (nothing granted yet) no credential bundle.
            initial_sync_ok = poll_until(
                lambda: _run_on_vps(ssh_config_path, f"test -f {_VPS_PERMISSIONS_PATH}").returncode == 0,
                timeout=_SYNC_CONVERGENCE_TIMEOUT_SECONDS,
                poll_interval=_POLL_INTERVAL_SECONDS,
            )
            assert initial_sync_ok, f"initial permissions sync never wrote {_VPS_PERMISSIONS_PATH} on the VPS"
            initial_permissions = _run_on_vps(ssh_config_path, f"cat {_VPS_PERMISSIONS_PATH}")
            assert _GRANTED_SCOPE not in initial_permissions.stdout, (
                f"VPS permissions already contain {_GRANTED_SCOPE} before the grant:\n{initial_permissions.stdout}"
            )
            assert _run_on_vps(ssh_config_path, f"test -f {_VPS_CREDENTIALS_PATH}").returncode != 0, (
                "VPS credential bundle exists before any service was granted"
            )

            _grant_scope_in_host_permissions(latchkey_directory, host_id)

            permissions_synced = poll_until(
                lambda: _GRANTED_SCOPE in _run_on_vps(ssh_config_path, f"cat {_VPS_PERMISSIONS_PATH}").stdout,
                timeout=_SYNC_CONVERGENCE_TIMEOUT_SECONDS,
                poll_interval=_POLL_INTERVAL_SECONDS,
            )
            assert permissions_synced, (
                f"granting {_GRANTED_SCOPE} locally never propagated to {_VPS_PERMISSIONS_PATH} on the VPS; "
                f"last content:\n{_run_on_vps(ssh_config_path, f'cat {_VPS_PERMISSIONS_PATH}').stdout}\n"
                f"forward log:\n{forward_log_path.read_text()}"
            )
            credentials_synced = poll_until(
                lambda: _run_on_vps(ssh_config_path, f"test -f {_VPS_CREDENTIALS_PATH}").returncode == 0,
                timeout=_SYNC_CONVERGENCE_TIMEOUT_SECONDS,
                poll_interval=_POLL_INTERVAL_SECONDS,
            )
            assert credentials_synced, (
                f"granting {_GRANTED_SCOPE} locally never shipped the credential bundle to "
                f"{_VPS_CREDENTIALS_PATH} on the VPS\nforward log:\n{forward_log_path.read_text()}"
            )
        finally:
            # Best-effort teardown, most-dependent first: workspace, forward
            # supervisor (SIGTERM -> coupled gateway shutdown), VPS-side state,
            # then any leaked containers for this test's prefix.
            if agent_address is not None:
                _run_mngr(env, repo, "destroy", agent_address, "--force", timeout=_QUICK_MNGR_TIMEOUT_SECONDS)
            if forward_process is not None:
                forward_process.terminate()
                try:
                    forward_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    forward_process.kill()
                    forward_process.wait()
            _run_on_vps(
                ssh_config_path,
                "supervisorctl stop latchkey-gateway latchkey-tunnel >/dev/null 2>&1; "
                f"rm -f {_VPS_SUPERVISOR_CONF_GLOB}; "
                "supervisorctl reread >/dev/null 2>&1; supervisorctl update >/dev/null 2>&1; "
                f"rm -rf {_VPS_LATCHKEY_DIR} /run/mngr-latchkey",
            )
            _cleanup_test_containers()


def _cleanup_test_containers() -> None:
    """Force-remove any Docker containers/volumes left behind under this test's MNGR_PREFIX.

    The 'remote' daemon is really the local one, so the plain docker CLI (no
    DOCKER_HOST) sees everything the test created. Failures are tolerated:
    ``mngr destroy`` is the primary cleanup; this is the safety net.
    """
    prefix = os.environ.get("MNGR_PREFIX")
    if not prefix:
        return
    for list_args, remove_args in (
        (["docker", "ps", "-aq", "--filter", f"name={prefix}"], ["docker", "rm", "-f"]),
        (["docker", "volume", "ls", "-q", "--filter", f"name={prefix}"], ["docker", "volume", "rm", "-f"]),
    ):
        try:
            listing = subprocess.run(list_args, capture_output=True, text=True, timeout=60)
            names = [line for line in listing.stdout.split() if line]
            if listing.returncode == 0 and names:
                subprocess.run([*remove_args, *names], capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, OSError):
            continue
