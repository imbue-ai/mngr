import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.encryption_key import encryption_key_path
from imbue.mngr_latchkey.remote_gateway import INNER_PORT
from imbue.mngr_latchkey.remote_gateway import LATCHKEY_VERSION
from imbue.mngr_latchkey.remote_gateway import OUTER_PORT
from imbue.mngr_latchkey.remote_gateway import RemoteGatewayError
from imbue.mngr_latchkey.remote_gateway import _GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote_gateway import _MINIMUM_NODE_MAJOR_VERSION
from imbue.mngr_latchkey.remote_gateway import _TUNNEL_PROGRAM_NAME
from imbue.mngr_latchkey.remote_gateway import _build_supervisor_program_config
from imbue.mngr_latchkey.remote_gateway import _ensure_container_tunnel_keypair
from imbue.mngr_latchkey.remote_gateway import _ensure_latchkey_gateway_reachable_from_container
from imbue.mngr_latchkey.remote_gateway import _ensure_latchkey_gateway_running
from imbue.mngr_latchkey.remote_gateway import _ensure_latchkey_installed
from imbue.mngr_latchkey.remote_gateway import _migrate_legacy_remote_gateway_state
from imbue.mngr_latchkey.remote_gateway import provision_remote_gateway
from imbue.mngr_latchkey.remote_gateway import sync_credentials
from imbue.mngr_latchkey.remote_gateway import sync_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir


class _Recorded(MutableModel):
    """One recorded ``execute_idempotent_command`` invocation."""

    command: str = Field(description="The command string passed to the outer host")
    timeout_seconds: float | None = Field(default=None, description="Timeout passed in (if any)")


class _WrittenFile(MutableModel):
    """One recorded ``write_file`` / ``write_text_file`` invocation."""

    path: str = Field(description="Destination path on the VPS")
    content: bytes = Field(description="Bytes written")
    mode: str | None = Field(default=None, description="chmod mode requested (if any)")
    is_atomic: bool = Field(default=False, description="Whether the write was requested atomically (tmp + rename)")


class _StubOuter(MutableModel):
    """Stub outer host that records commands / writes and returns a canned result.

    Implements only the subset of ``OuterHostInterface`` that the functions
    under test touch (``execute_idempotent_command``, ``write_file``,
    ``write_text_file``, ``get_name``).
    """

    name: str = Field(default="vps-test", description="Display name returned by get_name")
    result: CommandResult = Field(
        default_factory=lambda: CommandResult(stdout="", stderr="", success=True),
        description="Canned result returned for every command",
    )
    home: str = Field(default="/root", description="Value returned for the $HOME resolution command")
    container_name: str = Field(default="mngr-ws", description="Container name returned for the 'docker ps' lookup")
    is_local: bool = Field(default=False, description="Whether this outer host is the local machine")
    recorded: list[_Recorded] = Field(default_factory=list, description="Each command recorded in order")
    written: list[_WrittenFile] = Field(default_factory=list, description="Each file write recorded in order")

    def get_name(self) -> str:
        return self.name

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.recorded.append(_Recorded(command=command, timeout_seconds=timeout_seconds))
        # Only the dedicated $HOME-resolution probe gets the home response; the
        # container lookup returns the configured name; everything else
        # (install/gateway/keypair/tunnel scripts) returns the configured result.
        if command.strip() == 'echo "$HOME"':
            return CommandResult(stdout=f"{self.home}\n", stderr="", success=True)
        if command.startswith("docker ps"):
            return CommandResult(stdout=f"{self.container_name}\n", stderr="", success=True)
        return self.result

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = False) -> None:
        self.written.append(_WrittenFile(path=str(path), content=content, mode=mode, is_atomic=is_atomic))

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
    ) -> None:
        self.written.append(_WrittenFile(path=str(path), content=content.encode(encoding), mode=mode))


def _outer(result: CommandResult, name: str = "vps-test") -> OuterHostInterface:
    """Build a stub outer host typed as ``OuterHostInterface``.

    ``cast`` is used because the stub is structurally-but-not-nominally an
    OuterHostInterface (the interface has many other abstract methods that the
    function under test never calls).
    """
    return cast(OuterHostInterface, _StubOuter(name=name, result=result))


def _stub(outer: OuterHostInterface) -> _StubOuter:
    return cast(_StubOuter, outer)


def test_ensure_latchkey_installed_issues_single_idempotent_command() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_installed(outer)
    assert len(_stub(outer).recorded) == 1


def test_ensure_latchkey_installed_pins_the_version_in_the_npm_install() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_installed(outer)
    command = _stub(outer).recorded[0].command
    assert f"npm install -g latchkey@{LATCHKEY_VERSION}" in command
    # Reinstall is gated behind a version mismatch check, not unconditional.
    assert f'!= "{LATCHKEY_VERSION}"' in command


def test_ensure_latchkey_installed_gates_each_component_behind_a_presence_check() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_installed(outer)
    command = _stub(outer).recorded[0].command
    assert "command -v curl" in command
    # Node.js is gated behind a *version* probe, not mere presence: a
    # preinstalled distro node (e.g. Debian bookworm's 18.x) exists but cannot
    # run the pinned latchkey/npm, so it must trigger the NodeSource install.
    assert "node --version" in command
    assert f'[ "$_node_major" -lt {_MINIMUM_NODE_MAJOR_VERSION} ]' in command
    assert "command -v npm" in command
    # supervisord supervises the gateway + tunnel; installed only when missing,
    # and its init service is enabled so it auto-starts on boot.
    assert "command -v supervisord" in command
    assert "apt-get install -y supervisor" in command
    assert "systemctl enable --now supervisor" in command
    # supervisord replaces the old PID-file idempotency guard, so we still never
    # need procps.
    assert "procps" not in command
    # Version-agnostic: the NodeSource setup URL is present (the major version
    # is a tunable constant, so don't pin it here).
    assert "deb.nodesource.com/setup_" in command
    assert "apt-get install -y nodejs" in command
    # POSIX sh compatibility: must not rely on bash-only pipefail.
    assert "pipefail" not in command
    assert command.startswith("set -e")


def test_ensure_latchkey_installed_uses_generous_install_timeout() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_installed(outer)
    assert _stub(outer).recorded[0].timeout_seconds == 300.0


def test_ensure_latchkey_installed_raises_on_failure_with_stderr_in_message() -> None:
    outer = _outer(CommandResult(stdout="", stderr="E: Unable to locate package nodejs", success=False))
    with pytest.raises(RemoteGatewayError, match="Unable to locate package nodejs"):
        _ensure_latchkey_installed(outer)


def test_ensure_latchkey_installed_falls_back_to_stdout_when_stderr_empty() -> None:
    outer = _outer(CommandResult(stdout="npm ERR! network timeout", stderr="", success=False))
    with pytest.raises(RemoteGatewayError, match="npm ERR! network timeout"):
        _ensure_latchkey_installed(outer)


def _make_reencrypt_latchkey_binary(tmp_path: Path) -> Path:
    """Build a fake ``latchkey`` covering ``services info --offline`` and ``auth re-encrypt``.

    ``services info <svc> --offline`` reports ``missing`` for any service
    named in the ``FAKE_MISSING_SERVICES`` env var (comma-separated) and
    ``valid`` otherwise. ``auth re-encrypt`` writes ``credentials.json.enc``
    into the destination *directory* as JSON recording the requested service
    names (matching the real CLI, which takes an output directory). Together
    these let ``sync_credentials`` be exercised end-to-end without a real
    ``auth re-encrypt`` implementation.
    """
    script = tmp_path / "fake-latchkey"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        'if sys.argv[1:3] == ["services", "info"]:\n'
        "    service = sys.argv[3]\n"
        "    missing = os.environ.get('FAKE_MISSING_SERVICES', '').split(',')\n"
        # latchkey 3.0.0: a missing service has an empty ``credentials`` object;
        # a present service reports one account keyed by the empty string.
        "    credentials = {} if service in missing else {'': {'credentialType': 'rawCurl', 'credentialStatus': 'valid'}}\n"
        "    print(json.dumps({'credentials': credentials}))\n"
        "    sys.exit(0)\n"
        'assert sys.argv[1:3] == ["auth", "re-encrypt"], sys.argv\n'
        "rest = sys.argv[4:]\n"
        "services = rest[1:] if rest[:1] == ['--services'] else []\n"
        "out = os.path.join(sys.argv[3], 'credentials.json.enc')\n"
        "open(out, 'w').write(json.dumps({'services': services}))\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    return script


def _latchkey_with_fake_reencrypt(tmp_path: Path) -> Latchkey:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    return Latchkey(
        latchkey_directory=latchkey_directory,
        latchkey_binary=str(_make_reencrypt_latchkey_binary(tmp_path)),
    )


def _grant_permissions(latchkey: Latchkey, host_id: HostId, rules_json: str) -> None:
    permissions_path = permissions_path_for_host(plugin_data_dir(latchkey.latchkey_directory), host_id)
    permissions_path.parent.mkdir(parents=True)
    permissions_path.write_text(rules_json)


def test_sync_credentials_ships_only_services_the_host_is_granted(tmp_path: Path) -> None:
    latchkey = _latchkey_with_fake_reencrypt(tmp_path)
    host_id = HostId.generate()
    # Grant the host the slack scope; the catalog maps ``slack-api`` -> ``slack``.
    _grant_permissions(latchkey, host_id, '{"rules": [{"slack-api": ["slack-read-all"]}]}')
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_credentials(outer, latchkey, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/credentials.json.enc"
    # Only the granted service's credentials are exported, not the full store.
    assert json.loads(written[0].content.decode("utf-8"))["services"] == ["slack"]
    assert written[0].mode == "0600"
    # Written atomically (tmp + rename) so the remote gateway never reads a partial file.
    assert written[0].is_atomic is True


def test_sync_credentials_excludes_services_without_stored_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latchkey = _latchkey_with_fake_reencrypt(tmp_path)
    host_id = HostId.generate()
    # Grant both slack and github (github-rest-api -> github in the catalog).
    _grant_permissions(
        latchkey, host_id, '{"rules": [{"slack-api": ["slack-read-all"]}, {"github-rest-api": ["any"]}]}'
    )
    # Slack is granted but has no stored credentials; it must be dropped.
    monkeypatch.setenv("FAKE_MISSING_SERVICES", "slack")
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_credentials(outer, latchkey, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert json.loads(written[0].content.decode("utf-8"))["services"] == ["github"]


def test_sync_credentials_clears_remote_store_for_deny_all_host(tmp_path: Path) -> None:
    latchkey = _latchkey_with_fake_reencrypt(tmp_path)
    host_id = HostId.generate()
    # No permissions file -> deny-all -> nothing to ship; the remote store is cleared.
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_credentials(outer, latchkey, host_id)

    assert _stub(outer).written == []
    rm_commands = [r.command for r in _stub(outer).recorded if r.command.startswith("rm -f")]
    assert rm_commands == ["rm -f /root/.latchkey/credentials.json.enc"]


def test_sync_credentials_clears_remote_store_when_all_services_lack_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latchkey = _latchkey_with_fake_reencrypt(tmp_path)
    host_id = HostId.generate()
    _grant_permissions(latchkey, host_id, '{"rules": [{"slack-api": ["slack-read-all"]}]}')
    # The only granted service has no stored credentials -> nothing to ship.
    monkeypatch.setenv("FAKE_MISSING_SERVICES", "slack")
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_credentials(outer, latchkey, host_id)

    assert _stub(outer).written == []
    assert any(r.command.startswith("rm -f") for r in _stub(outer).recorded)


def test_sync_credentials_raises_when_reencrypt_fails(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    failing_binary = tmp_path / "fake-latchkey"
    # ``services info`` succeeds (so the granted service is kept), but
    # ``auth re-encrypt`` fails so we exercise the export-failure path.
    failing_binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        'if sys.argv[1:3] == ["services", "info"]:\n'
        "    print(json.dumps({'credentials': {'': {'credentialType': 'rawCurl', 'credentialStatus': 'valid'}}}))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    failing_binary.chmod(0o755)
    latchkey = Latchkey(latchkey_directory=latchkey_directory, latchkey_binary=str(failing_binary))
    host_id = HostId.generate()
    _grant_permissions(latchkey, host_id, '{"rules": [{"slack-api": ["slack-read-all"]}]}')
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    with pytest.raises(RemoteGatewayError, match="export filtered latchkey credentials"):
        sync_credentials(outer, latchkey, host_id)


def test_sync_permissions_copies_per_host_file_to_remote_permissions_json(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    local_path.parent.mkdir(parents=True)
    local_path.write_text('{"rules": [{"slack-api": ["slack-read-all"]}]}')
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/permissions.json"
    assert b"slack-read-all" in written[0].content
    assert written[0].mode == "0600"
    # Written atomically (tmp + rename) so the remote gateway never reads a partial file.
    assert written[0].is_atomic is True


def test_sync_permissions_falls_back_to_restrictive_default_when_local_missing(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = _outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id)

    written = _stub(outer).written
    assert len(written) == 1
    assert written[0].path == "/root/.latchkey/permissions.json"
    # The deny-all default carries an empty rules list and no schemas block.
    assert written[0].content == b'{\n  "rules": []\n}'


def test_sync_permissions_resolves_remote_home_for_the_destination(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _StubOuter(home="/home/agent"))

    sync_permissions(outer, latchkey_directory, host_id)

    assert _stub(outer).written[0].path == "/home/agent/.latchkey/permissions.json"


def test_sync_permissions_raises_when_home_resolution_fails(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = cast(OuterHostInterface, _StubOuter(home=""))

    with pytest.raises(RemoteGatewayError, match="resolve \\$HOME"):
        sync_permissions(outer, latchkey_directory, host_id)


def test_ports_are_integers() -> None:
    assert isinstance(INNER_PORT, int)
    assert isinstance(OUTER_PORT, int)


def _written_by_path(outer: OuterHostInterface, path: str) -> _WrittenFile:
    """Return the single recorded file write for ``path`` (asserting exactly one)."""
    matches = [w for w in _stub(outer).written if w.path == path]
    assert len(matches) == 1, (path, [w.path for w in _stub(outer).written])
    return matches[0]


def _gateway_run_script(outer: OuterHostInterface) -> str:
    """Return the content of the gateway launch wrapper written to the VPS."""
    return _written_by_path(outer, "/root/.latchkey/gateway_run.sh").content.decode("utf-8")


def _gateway_conf(outer: OuterHostInterface) -> str:
    """Return the content of the gateway supervisord drop-in written to the VPS."""
    return _written_by_path(outer, f"/etc/supervisor/conf.d/{_GATEWAY_PROGRAM_NAME}.conf").content.decode("utf-8")


def _reload_commands(outer: OuterHostInterface) -> list[str]:
    return [r.command for r in _stub(outer).recorded if "supervisorctl" in r.command]


def test_build_supervisor_program_config_escapes_percent_for_supervisord_interpolation() -> None:
    # supervisord expands %(...)s in every value before shell-splitting the
    # command, so a literal % (here in an exotic path) must be doubled to %%,
    # otherwise supervisord fails to parse the config.
    conf = _build_supervisor_program_config(
        "latchkey-gateway",
        "/bin/sh '/tmp/50%off/gateway_run.sh'",
        "/tmp/50%off/gateway.log",
        3,
    )
    assert "command=/bin/sh '/tmp/50%%off/gateway_run.sh'" in conf
    assert "stdout_logfile=/tmp/50%%off/gateway.log" in conf
    # No lone (un-doubled) percent survives, which would break config parsing.
    assert conf.count("%") == conf.count("%%") * 2


def test_ensure_latchkey_gateway_running_registers_supervisord_program_on_outer_port_loopback(
    tmp_path: Path,
) -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, "shared-password")
    run_script = _gateway_run_script(outer)
    conf = _gateway_conf(outer)
    # The wrapper exports the gateway config and execs the gateway. Gateway
    # binds OUTER_PORT on loopback, with counting disabled.
    assert f"export LATCHKEY_GATEWAY_PORT={OUTER_PORT}" in run_script
    assert "export LATCHKEY_GATEWAY_LISTEN_HOST=127.0.0.1" in run_script
    assert "export LATCHKEY_DISABLE_COUNTING=1" in run_script
    # Credential refresh is disabled so the VPS gateway never rotates the
    # user's OAuth token: it runs on a synced copy and the desktop-side
    # latchkey remains the single owner of credential refresh.
    assert "export LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1" in run_script
    # exec so supervisord tracks the gateway PID directly, not a wrapping shell,
    # with the same body-size limit the desktop-side gateway uses.
    assert f"exec latchkey gateway --max-body-size {GATEWAY_MAX_BODY_SIZE_BYTES}" in run_script
    # The encryption key and listen password are read from 0600 files into the
    # environment (not interpolated), so the literal secret never appears.
    assert 'LATCHKEY_ENCRYPTION_KEY="$(cat ' in run_script
    assert 'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(cat ' in run_script
    assert "export LATCHKEY_ENCRYPTION_KEY LATCHKEY_GATEWAY_LISTEN_PASSWORD" in run_script
    assert "shared-password" not in run_script
    # The wrapper refuses to launch a keyless gateway when its tmpfs secrets are
    # gone (e.g. wiped by a reboot).
    assert "exit 1" in run_script
    assert "awaiting re-provision" in run_script
    # supervisord keeps it up: autostart + autorestart on crash.
    assert f"[program:{_GATEWAY_PROGRAM_NAME}]" in conf
    assert "autostart=true" in conf
    assert "autorestart=true" in conf
    assert "/bin/sh /root/.latchkey/gateway_run.sh" in conf
    # Applied via reread + update + best-effort start; no more nohup/pidfile launch.
    assert _reload_commands(outer) == [
        f"supervisorctl reread && supervisorctl update && (supervisorctl start {_GATEWAY_PROGRAM_NAME} || true)"
    ]
    assert all("nohup" not in r.command for r in _stub(outer).recorded)


def test_ensure_latchkey_gateway_running_writes_secrets_to_0600_tmpfs_files(tmp_path: Path) -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, "shared-password")
    # Secrets go in a RAM-backed dir under /run, never on the persistent disk
    # beside the encrypted credential store; the wrapper stays on the normal disk.
    key_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_encryption_key")
    password_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_listen_password")
    run_file = _written_by_path(outer, "/root/.latchkey/gateway_run.sh")
    # The password file's content is the literal secret; it is never written to
    # a command (see the wrapper test above).
    assert password_file.content == b"shared-password"
    # Secrets are 0600; the wrapper is executable (0700).
    assert key_file.mode == "0600"
    assert password_file.mode == "0600"
    assert run_file.mode == "0700"
    # The wrapper reads back exactly the two tmpfs secret file paths.
    run_script = run_file.content.decode("utf-8")
    assert key_file.path in run_script
    assert password_file.path in run_script


def test_ensure_latchkey_gateway_running_injects_local_encryption_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the local key (and clear any operator override) so the exact value is
    # written to the encryption-key file (and never to a command).
    monkeypatch.delenv("LATCHKEY_ENCRYPTION_KEY", raising=False)
    key_path = encryption_key_path(tmp_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("my-test-key-abc123")
    os.chmod(key_path, 0o600)
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, "shared-password")
    key_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_encryption_key")
    assert key_file.content == b"my-test-key-abc123"
    # The key never appears in any recorded command string.
    assert all("my-test-key-abc123" not in r.command for r in _stub(outer).recorded)


def test_ensure_latchkey_gateway_running_verifies_secrets_dir_is_ram_backed(tmp_path: Path) -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, "shared-password")
    # Before writing the key, provisioning creates the /run secrets dir (0700)
    # and asserts its filesystem is RAM-backed (tmpfs/ramfs), refusing to
    # persist the key to disk otherwise.
    guard_commands = [
        r.command for r in _stub(outer).recorded if "stat -f -c %T" in r.command and "/run/mngr-latchkey" in r.command
    ]
    assert len(guard_commands) == 1, guard_commands
    guard = guard_commands[0]
    assert "mkdir -p /run/mngr-latchkey" in guard
    assert "chmod 700 /run/mngr-latchkey" in guard
    assert '[ "$_fstype" != tmpfs ]' in guard
    assert '[ "$_fstype" != ramfs ]' in guard


def test_ensure_latchkey_gateway_running_raises_when_secrets_dir_not_ram_backed(tmp_path: Path) -> None:
    # The first real command is the RAM-backed-dir guard; a failure there (e.g.
    # /run is not a tmpfs) must abort before the key is ever written.
    outer = _outer(CommandResult(stdout="", stderr="is on a ext4 filesystem", success=False))
    with pytest.raises(RemoteGatewayError, match="RAM-backed secrets directory"):
        _ensure_latchkey_gateway_running(outer, tmp_path, "shared-password")
    # Crucially, no secret file was written when the guard failed.
    assert _stub(outer).written == []


def _tunnel_conf(outer: OuterHostInterface) -> str:
    """Return the content of the reverse-tunnel supervisord drop-in written to the VPS."""
    return _written_by_path(outer, f"/etc/supervisor/conf.d/{_TUNNEL_PROGRAM_NAME}.conf").content.decode("utf-8")


def test_ensure_latchkey_gateway_reachable_registers_reverse_tunnel_program() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_reachable_from_container(
        outer,
        container_ssh_user="root",
        container_ssh_port=2222,
        container_ssh_key_path=Path("/etc/mngr/container_key"),
    )
    conf = _tunnel_conf(outer)
    # supervisord program that reverse-forwards the container's INNER_PORT
    # loopback to the VPS gateway's OUTER_PORT, restarted on exit.
    assert f"[program:{_TUNNEL_PROGRAM_NAME}]" in conf
    assert "autorestart=true" in conf
    assert f"-R 127.0.0.1:{INNER_PORT}:127.0.0.1:{OUTER_PORT}" in conf
    # SSHes into the published container sshd over VPS loopback, as the given
    # user, via an absolute ssh path (supervisord resolves via its own PATH).
    assert "/usr/bin/ssh" in conf
    assert "-p 2222" in conf
    assert "-i /etc/mngr/container_key" in conf
    assert "root@127.0.0.1" in conf
    # Runs in the foreground under supervisord (no nohup / ssh -f), fails to
    # bind loudly, and carries keepalive flags so a hung connection (e.g. a
    # resumed VM) is detected and torn down, prompting a supervisord restart.
    assert "nohup" not in conf
    assert "ssh -f" not in conf
    assert "ExitOnForwardFailure=yes" in conf
    assert "ServerAliveInterval=30" in conf
    assert "ServerAliveCountMax=3" in conf
    assert "TCPKeepAlive=yes" in conf
    # Applied via reread + update + best-effort start.
    assert _reload_commands(outer) == [
        f"supervisorctl reread && supervisorctl update && (supervisorctl start {_TUNNEL_PROGRAM_NAME} || true)"
    ]


def test_ensure_latchkey_gateway_reachable_quotes_key_path_with_spaces() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_reachable_from_container(
        outer,
        container_ssh_user="root",
        container_ssh_port=2222,
        container_ssh_key_path=Path("/tmp/key dir/id_ed25519"),
    )
    conf = _tunnel_conf(outer)
    assert "-i '/tmp/key dir/id_ed25519'" in conf


def test_ensure_latchkey_gateway_reachable_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="supervisorctl: command not found", success=False))
    with pytest.raises(RemoteGatewayError, match="reload supervisor"):
        _ensure_latchkey_gateway_reachable_from_container(
            outer,
            container_ssh_user="root",
            container_ssh_port=2222,
            container_ssh_key_path=Path("/etc/mngr/container_key"),
        )


def test_ensure_container_tunnel_keypair_generates_key_and_authorizes_in_container() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    key_path = _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="root")
    # Private key lands under the resolved remote latchkey dir.
    assert key_path == Path("/root/.latchkey/container_tunnel_key")
    script = _stub(outer).recorded[-1].command
    # Generates the keypair (only when absent) and authorizes it via docker exec.
    assert "ssh-keygen -t ed25519 -N '' -q -f /root/.latchkey/container_tunnel_key" in script
    assert "if [ ! -f /root/.latchkey/container_tunnel_key ]; then" in script
    assert "docker exec -u root" in script
    assert "mngr-ws" in script
    # Public key is passed via env, not spliced into the inner command.
    assert 'TUNNEL_PUBKEY="$(cat /root/.latchkey/container_tunnel_key.pub)"' in script
    assert "-e TUNNEL_PUBKEY=" in script
    # Idempotent authorized_keys append.
    assert "grep -qxF" in script
    assert "authorized_keys" in script


def test_ensure_container_tunnel_keypair_returns_path_under_resolved_home() -> None:
    outer = cast(OuterHostInterface, _StubOuter(home="/home/agent"))
    key_path = _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="agent")
    assert key_path == Path("/home/agent/.latchkey/container_tunnel_key")
    assert "docker exec -u agent" in _stub(outer).recorded[-1].command


def test_ensure_container_tunnel_keypair_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="Error: No such container: mngr-ws", success=False))
    with pytest.raises(RemoteGatewayError, match="No such container"):
        _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="root")


def test_provision_remote_gateway_runs_full_sequence_on_the_outer_host(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, _StubOuter(container_name="mngr-ws-1"))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        gateway_password="shared-password",
    )
    commands = "\n\n".join(r.command for r in _stub(outer).recorded)
    written = "\n\n".join(w.content.decode("utf-8", "replace") for w in _stub(outer).written)
    # Install latchkey + supervisor, find the container, mint+authorize a key,
    # and register the gateway + reverse-tunnel supervisord programs.
    assert "npm install -g latchkey@" in commands
    assert "apt-get install -y supervisor" in commands
    assert "docker ps -a --filter" in commands
    assert "com.imbue.mngr.host-id=" in commands
    assert "ssh-keygen -t ed25519" in commands
    assert "docker exec -u root" in commands
    assert "mngr-ws-1" in commands
    assert "supervisorctl reread && supervisorctl update && (supervisorctl start" in commands
    # A legacy (nohup + PID-file) gateway/tunnel is torn down *before* the new
    # supervisord programs are applied, so it frees OUTER_PORT / the container
    # forward bind first.
    assert '"$HOME/.latchkey/gateway.pid"' in commands
    assert commands.index('"$HOME/.latchkey/gateway.pid"') < commands.index("supervisorctl reread")
    # Both supervisord programs (gateway + tunnel) were written.
    assert f"[program:{_GATEWAY_PROGRAM_NAME}]" in written
    assert f"[program:{_TUNNEL_PROGRAM_NAME}]" in written
    assert "exec latchkey gateway" in written
    assert "-R 127.0.0.1:" in written
    # The gateway listen password is written to a file, never a command.
    assert "shared-password" not in commands
    password_files = [w for w in _stub(outer).written if w.content == b"shared-password"]
    assert len(password_files) == 1


def test_migrate_legacy_remote_gateway_state_kills_pidfile_processes_and_scrubs_secrets() -> None:
    outer = _outer(CommandResult(stdout="", stderr="", success=True))
    _migrate_legacy_remote_gateway_state(outer)
    assert len(_stub(outer).recorded) == 1
    script = _stub(outer).recorded[0].command
    # Kills the legacy nohup gateway + tunnel by their PID files, each guarded by
    # a /proc cmdline marker so a reused PID is never signalled (TERM then KILL
    # so the held port/forward is freed before the supervisord replacement).
    assert '"$HOME/.latchkey/gateway.pid"' in script
    assert '"$HOME/.latchkey/tunnel.pid"' in script
    assert "grep -qaF gateway " in script
    assert f"grep -qaF 127.0.0.1:{INNER_PORT}:127.0.0.1:{OUTER_PORT} " in script
    assert 'kill "$_pid"' in script
    assert 'kill -9 "$_pid"' in script
    assert 'rm -f "$_pidfile"' in script
    # Scrubs any on-disk secrets an intermediate build persisted (now tmpfs-only);
    # double-quoted so $HOME expands (shlex.quote would stop it).
    assert 'rm -f "$HOME/.latchkey/gateway_encryption_key" "$HOME/.latchkey/gateway_listen_password"' in script


def test_migrate_legacy_remote_gateway_state_raises_on_failure() -> None:
    outer = _outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(RemoteGatewayError, match="migrate legacy latchkey gateway"):
        _migrate_legacy_remote_gateway_state(outer)


def test_provision_remote_gateway_raises_when_container_not_found(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, _StubOuter(container_name=""))
    with pytest.raises(RemoteGatewayError, match="No container labeled"):
        provision_remote_gateway(
            outer,
            host_id=HostId(),
            container_ssh_user="root",
            container_ssh_port=2222,
            latchkey_directory=tmp_path,
            gateway_password="shared-password",
        )


def test_provision_remote_gateway_is_noop_on_local_outer_host(tmp_path: Path) -> None:
    # A local outer (e.g. the local docker daemon's machine) must never be
    # provisioned -- we don't apt/npm-install latchkey on the user's computer.
    outer = cast(OuterHostInterface, _StubOuter(is_local=True))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        gateway_password="shared-password",
    )
    assert _stub(outer).recorded == []
