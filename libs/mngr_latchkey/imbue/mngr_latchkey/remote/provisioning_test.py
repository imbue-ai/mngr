import json
import re
import stat
from pathlib import Path
from typing import cast

import pytest
from packaging.version import Version
from pydantic import SecretStr

from imbue.mngr.hosts.outer_host import OuterHost
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.additional_services import additional_service_registration_entries
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
from imbue.mngr_latchkey.core import LATCHKEY_MIN_VERSION
from imbue.mngr_latchkey.core import REMOTE_GATEWAY_EXTENSION_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_FIREWALL_UNIT_NAME
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.owner_exec_vm import VM_EXEC_PORT
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_gateway_password
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import stored_machine_gateway_password
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import DEFAULT_DOCKER_BRIDGE_ADDRESS
from imbue.mngr_latchkey.remote.mock_outer_host_test import MACHINE_KEY
from imbue.mngr_latchkey.remote.mock_outer_host_test import StubOuter
from imbue.mngr_latchkey.remote.mock_outer_host_test import WrittenFile
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_stub
from imbue.mngr_latchkey.remote.mock_outer_host_test import stub_outer
from imbue.mngr_latchkey.remote.provisioning import CONTAINER_TUNNEL_KEY_FILENAME
from imbue.mngr_latchkey.remote.provisioning import DATALIB_CURL_VERSION
from imbue.mngr_latchkey.remote.provisioning import DESKTOP_GATEWAY_VPS_PORT
from imbue.mngr_latchkey.remote.provisioning import DesktopGatewaySecrets
from imbue.mngr_latchkey.remote.provisioning import GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote.provisioning import GATEWAY_RUN_SCRIPT_FILENAME
from imbue.mngr_latchkey.remote.provisioning import LATCHKEY_VERSION
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_DISK_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_GATEWAY_REQUIRED_TMPFS_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import MACHINE_LATCHKEY_TMPFS_FILENAMES
from imbue.mngr_latchkey.remote.provisioning import OUTER_PORT
from imbue.mngr_latchkey.remote.provisioning import REMOTE_EXTENSIONS_DIR_NAME
from imbue.mngr_latchkey.remote.provisioning import SUPERVISOR_CONFD_DIR
from imbue.mngr_latchkey.remote.provisioning import TMPFS_SECRETS_DIR
from imbue.mngr_latchkey.remote.provisioning import TUNNEL_PROGRAM_NAME
from imbue.mngr_latchkey.remote.provisioning import _CURL_DISPATCH_PATH
from imbue.mngr_latchkey.remote.provisioning import _CURL_IMPERSONATE_PATH
from imbue.mngr_latchkey.remote.provisioning import _CURL_STAGED_SUFFIX
from imbue.mngr_latchkey.remote.provisioning import _CURL_VERSION_STAMP_PATH
from imbue.mngr_latchkey.remote.provisioning import _MINIMUM_NODE_MAJOR_VERSION
from imbue.mngr_latchkey.remote.provisioning import _REMOTE_EXTENSION_CANDIDATE_SUFFIX
from imbue.mngr_latchkey.remote.provisioning import _build_extension_install_script
from imbue.mngr_latchkey.remote.provisioning import _build_supervisor_program_config
from imbue.mngr_latchkey.remote.provisioning import _does_container_need_reverse_tunnel
from imbue.mngr_latchkey.remote.provisioning import _does_container_resolve_outer_host
from imbue.mngr_latchkey.remote.provisioning import _does_key_open_the_machine_store
from imbue.mngr_latchkey.remote.provisioning import _ensure_bridge_services_firewalled
from imbue.mngr_latchkey.remote.provisioning import _ensure_container_tunnel_keypair
from imbue.mngr_latchkey.remote.provisioning import _ensure_latchkey_gateway_reachable_from_container
from imbue.mngr_latchkey.remote.provisioning import (
    _ensure_latchkey_gateway_running as _ensure_latchkey_gateway_running_real,
)
from imbue.mngr_latchkey.remote.provisioning import _migrate_legacy_remote_gateway_state
from imbue.mngr_latchkey.remote.provisioning import _resolve_bridge_listen_host
from imbue.mngr_latchkey.remote.provisioning import _resolve_machine_encryption_key
from imbue.mngr_latchkey.remote.provisioning import _resolve_machine_gateway_password
from imbue.mngr_latchkey.remote.provisioning import ensure_latchkey_installed
from imbue.mngr_latchkey.remote.provisioning import provision_remote_gateway
from imbue.mngr_latchkey.remote.provisioning import resolve_remote_latchkey_directory
from imbue.mngr_latchkey.remote.provisioning import sync_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Where the stub machine keeps its latchkey directory (its $HOME is /root).
_REMOTE_DIR = Path("/root/.latchkey")

# This computer's own gateway secrets, as every provisioning call here hands
# them over. Distinct strings from the machine's own listen password so a test
# can tell which secret landed where.
_DESKTOP_SECRETS = DesktopGatewaySecrets(
    gateway_password="desktop-password",
    permissions_override="desktop-override-jwt",
)


def _ensure_latchkey_gateway_running(
    outer: OuterHostInterface,
    latchkey_directory: Path,
    machine_encryption_key: str,
    machine_gateway_password: str,
) -> None:
    _ensure_latchkey_gateway_running_real(
        outer,
        DEFAULT_DOCKER_BRIDGE_ADDRESS,
        latchkey_directory,
        SecretStr(machine_encryption_key),
        machine_gateway_password,
        _DESKTOP_SECRETS,
    )


def test_minimum_version_is_not_newer_than_the_version_we_install() -> None:
    """A floor newer than the version we install means a half-finished bump.

    See the comment above :data:`LATCHKEY_MIN_VERSION` for why the pins move
    together.
    """
    assert Version(LATCHKEY_MIN_VERSION) <= Version(LATCHKEY_VERSION), (
        f"LATCHKEY_MIN_VERSION={LATCHKEY_MIN_VERSION} is newer than the installed "
        f"LATCHKEY_VERSION={LATCHKEY_VERSION}; raise the install pin to at least the minimum."
    )


def test_ensure_latchkey_installed_issues_single_idempotent_command() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    assert len(as_stub(outer).recorded) == 1


def test_ensure_latchkey_installed_pins_the_version_in_the_npm_install() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = as_stub(outer).recorded[0].command
    assert f"npm install -g latchkey@{LATCHKEY_VERSION}" in command
    # Reinstall is gated behind a version mismatch check, not unconditional.
    assert f'!= "{LATCHKEY_VERSION}"' in command


def test_ensure_latchkey_installed_gates_each_component_behind_a_presence_check() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = as_stub(outer).recorded[0].command
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


def test_ensure_latchkey_installed_installs_impersonating_curl() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = as_stub(outer).recorded[0].command
    # Fetches the datalib curl tarball for the VPS arch from the pinned
    # release, verifies it against the published .sha256, and installs both
    # the dispatch curl and the impersonator it fronts.
    assert f"releases/download/{DATALIB_CURL_VERSION}/" in command
    assert "curl-${_ci_triple}.tar.gz" in command
    # Every arch the script resolves lands on a statically linked musl build:
    # the glibc build only runs where glibc is at least as new as datalib's
    # build host, which rules out older VPS images. Checked over the whole set
    # of ``_ci_triple`` assignments so an arch branch added later cannot
    # quietly reintroduce a gnu triple.
    assert set(re.findall(r"_ci_triple=(\S+)", command)) == {
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
    }
    assert "sha256sum -c" in command
    assert "tar -xzf" in command and "--strip-components=1" in command
    # Both binaries are staged beside their destinations and then renamed into
    # place: overwriting them directly would truncate a file the running
    # gateway may be executing (ETXTBSY), and staging both before swapping
    # either leaves the previous pair untouched if the download, checksum, or
    # staging fails.
    staged_dispatch = f"{_CURL_DISPATCH_PATH}{_CURL_STAGED_SUFFIX}"
    staged_impersonate = f"{_CURL_IMPERSONATE_PATH}{_CURL_STAGED_SUFFIX}"
    assert f'install -m 0755 "${{_ci_tmp}}/{_CURL_DISPATCH_PATH.rsplit("/", 1)[1]}" "{staged_dispatch}"' in command
    assert (
        f'install -m 0755 "${{_ci_tmp}}/{_CURL_IMPERSONATE_PATH.rsplit("/", 1)[1]}" "{staged_impersonate}"' in command
    )
    assert f'mv -f "{staged_impersonate}" "{_CURL_IMPERSONATE_PATH}"' in command
    assert f'mv -f "{staged_dispatch}" "{_CURL_DISPATCH_PATH}"' in command
    # The dispatch curl -- the one LATCHKEY_CURL names -- is swapped last, so
    # no request reaches a new dispatch curl fronting an old impersonator.
    assert command.index(f'mv -f "{staged_impersonate}"') < command.index(f'mv -f "{staged_dispatch}"')
    # Version-gated, not presence-gated: the binaries are installed under
    # version-less names, so a VPS that already has an older release's pair
    # must be re-installed rather than skipped -- otherwise a bump only ever
    # reaches hosts that have never been provisioned.
    assert f"[ ! -x {_CURL_DISPATCH_PATH} ] || " in command
    assert f'[ "$(cat {_CURL_VERSION_STAMP_PATH} 2>/dev/null)" != "$_ci_want" ]; then' in command
    assert f'_ci_want="{DATALIB_CURL_VERSION} ${{_ci_triple}}"' in command
    # The stamp is written only after both binaries are swapped in, so an
    # aborted install never leaves a stamp claiming the new version.
    assert command.index(f'mv -f "{staged_dispatch}"') < command.index(f"> {_CURL_VERSION_STAMP_PATH}")
    # Fail-loud like the other components -- no best-effort warning fallback
    # that swallows failures.
    assert "latchkey will use system curl" not in command


def test_ensure_latchkey_installed_probes_installed_version_without_executing_latchkey() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = as_stub(outer).recorded[0].command
    # The probe must read the npm-installed package's package.json, not run the
    # CLI: since 2.x, ``latchkey --version`` resolves the encryption key (to
    # run its store migrations) before printing anything, so on a headless VPS
    # with an existing credential store and no key in the environment it exits
    # non-zero -- which read as a permanent version mismatch and reinstalled
    # latchkey on every provisioning pass.
    assert "latchkey --version" not in command
    assert '"$(npm root -g)/latchkey/package.json"' in command


def test_ensure_latchkey_installed_restarts_gateway_when_version_changed() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    command = as_stub(outer).recorded[0].command
    # A supervisord-managed gateway keeps the old code in memory across an npm
    # upgrade, so the install branch must bounce it -- tolerating hosts where
    # the program is not registered yet.
    restart_line = f"supervisorctl restart {GATEWAY_PROGRAM_NAME} || true"
    assert restart_line in command
    # The restart belongs inside the version-mismatch branch, after the
    # install itself.
    assert command.index("npm install -g latchkey@") < command.index(restart_line)


def test_ensure_latchkey_installed_uses_generous_install_timeout() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    ensure_latchkey_installed(outer)
    assert as_stub(outer).recorded[0].timeout_seconds == 300.0


def test_ensure_latchkey_installed_raises_on_failure_with_stderr_in_message() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="E: Unable to locate package nodejs", success=False))
    with pytest.raises(RemoteGatewayError, match="Unable to locate package nodejs"):
        ensure_latchkey_installed(outer)


def test_ensure_latchkey_installed_falls_back_to_stdout_when_stderr_empty() -> None:
    outer = stub_outer(CommandResult(stdout="npm ERR! network timeout", stderr="", success=False))
    with pytest.raises(RemoteGatewayError, match="npm ERR! network timeout"):
        ensure_latchkey_installed(outer)


def test_sync_permissions_seeds_a_machine_with_no_policy_from_the_local_file(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    local_path.parent.mkdir(parents=True)
    local_path.write_text('{"rules": [{"slack-api": ["slack-read-all"]}]}')
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id, _REMOTE_DIR)

    written = as_stub(outer).written
    # The permissions file is self-contained, so it is the only file that ships.
    assert len(written) == 1
    permissions = written[0]
    assert permissions.path == "/root/.latchkey/permissions.json"
    assert b"slack-read-all" in permissions.content
    assert permissions.mode == "0600"
    # Written atomically (tmp + rename) so the remote gateway never reads a partial file.
    assert permissions.is_atomic is True


def test_sync_permissions_falls_back_to_restrictive_default_when_local_missing(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    host_id = HostId.generate()
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))

    sync_permissions(outer, latchkey_directory, host_id, _REMOTE_DIR)

    written = as_stub(outer).written
    assert len(written) == 1
    # The deny-all default carries an empty rules list and no schemas block.
    assert written[0].content == b'{\n  "rules": []\n}'


def test_resolve_remote_latchkey_directory_resolves_the_machines_home(tmp_path: Path) -> None:
    """The reconcile resolves the machine's ~/.latchkey once and passes it to every step."""
    outer = cast(OuterHostInterface, StubOuter(home="/home/agent"))

    assert resolve_remote_latchkey_directory(outer) == Path("/home/agent/.latchkey")


def test_resolve_remote_latchkey_directory_raises_when_home_resolution_fails(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, StubOuter(home=""))

    with pytest.raises(RemoteGatewayError, match="resolve \\$HOME"):
        resolve_remote_latchkey_directory(outer)


def test_workspace_and_vps_gateway_ports_are_consistent() -> None:
    assert OUTER_PORT == AGENT_SIDE_LATCHKEY_PORT
    assert DESKTOP_GATEWAY_VPS_PORT != OUTER_PORT


def _written_by_path(outer: OuterHostInterface, path: str) -> WrittenFile:
    """Return the single recorded file write for ``path`` (asserting exactly one)."""
    matches = [w for w in as_stub(outer).written if w.path == path]
    assert len(matches) == 1, (path, [w.path for w in as_stub(outer).written])
    return matches[0]


def _gateway_run_script(outer: OuterHostInterface) -> str:
    """Return the content of the gateway launch wrapper written to the VPS."""
    return _written_by_path(outer, "/root/.latchkey/gateway_run.sh").content.decode("utf-8")


def _gateway_conf(outer: OuterHostInterface) -> str:
    """Return the content of the gateway supervisord drop-in written to the VPS."""
    return _written_by_path(outer, f"/etc/supervisor/conf.d/{GATEWAY_PROGRAM_NAME}.conf").content.decode("utf-8")


def _reload_commands(outer: OuterHostInterface) -> list[str]:
    return [r.command for r in as_stub(outer).recorded if r.command.startswith("supervisorctl reread")]


def _recorded_commands_text(outer: OuterHostInterface) -> str:
    """Every command run on the VPS, in order, as one searchable string."""
    return "\n\n".join(r.command for r in as_stub(outer).recorded)


def _written_content_text(outer: OuterHostInterface) -> str:
    """The content of every file written to the VPS, in order, as one searchable string."""
    return "\n\n".join(w.content.decode("utf-8", "replace") for w in as_stub(outer).written)


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


def test_ensure_latchkey_gateway_running_registers_supervisord_program_on_the_docker_bridge_address(
    tmp_path: Path,
) -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    run_script = _gateway_run_script(outer)
    conf = _gateway_conf(outer)
    # The wrapper exports the gateway config and execs the gateway. The gateway
    # binds OUTER_PORT on the docker bridge address only -- reachable from the
    # workspace container, never from off-host -- with counting disabled.
    # The listen port is named the way upstream reads it, so OUTER_PORT is what
    # the gateway actually binds rather than latchkey's default happening to match.
    assert f"export LATCHKEY_GATEWAY_LISTEN_PORT={OUTER_PORT}" in run_script
    assert f"export LATCHKEY_GATEWAY_LISTEN_HOST={DEFAULT_DOCKER_BRIDGE_ADDRESS}" in run_script
    assert "LISTEN_HOST=0.0.0.0" not in run_script
    assert "LISTEN_HOST=127.0.0.1" not in run_script
    assert "export LATCHKEY_DISABLE_COUNTING=1" in run_script
    # The machine renews its own tokens: the store it runs on is its own, so
    # nothing else holds the refresh tokens in it to race with, and it keeps
    # working while the user's computer is off.
    assert "LATCHKEY_DISABLE_CREDENTIALS_REFRESH" not in run_script
    assert f"export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL=http://127.0.0.1:{DESKTOP_GATEWAY_VPS_PORT}" in run_script
    proxy_candidate = _written_by_path(outer, "/root/.latchkey/extensions/desktop_gateway_proxy.mjs.candidate")
    assert b"Desktop latchkey gateway is unreachable" in proxy_candidate.content
    extension_install_command = next(
        record.command for record in as_stub(outer).recorded if "desktop_gateway_proxy.mjs.candidate" in record.command
    )
    assert "cmp -s" in extension_install_command
    assert "supervisorctl" not in extension_install_command
    # exec so supervisord tracks the gateway PID directly, not a wrapping shell,
    # with the same body-size limit the desktop-side gateway uses.
    assert f"exec latchkey gateway --max-body-size {GATEWAY_MAX_BODY_SIZE_BYTES}" in run_script
    # The machine's own encryption key and listen password are read from 0600
    # files into the environment (not interpolated), so the literal secret never
    # appears.
    assert 'LATCHKEY_ENCRYPTION_KEY="$(cat ' in run_script
    assert 'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(cat ' in run_script
    assert "export LATCHKEY_ENCRYPTION_KEY LATCHKEY_GATEWAY_LISTEN_PASSWORD" in run_script
    # The desktop-owned pair is handed to the forwarding extension as file
    # *paths*: it reads them per request, so a pass from another of the user's
    # computers takes effect without restarting this gateway.
    assert (
        "export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE=/run/mngr-latchkey/desktop_gateway_password"
        in run_script
    )
    assert (
        "export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE_FILE="
        "/run/mngr-latchkey/desktop_permissions_override" in run_script
    )
    assert "machine-password" not in run_script
    assert "desktop-password" not in run_script
    assert "desktop-override-jwt" not in run_script
    # Routes latchkey through the bundled dispatch curl. Unconditional export --
    # provisioning installs the pair fail-loud, so reaching this script at all
    # guarantees they are there. The dispatch curl finds the impersonator as a
    # sibling, so no second env var is exported.
    assert f"export LATCHKEY_CURL={_CURL_DISPATCH_PATH}" in run_script
    assert "FRANKWEILER_IMPERSONATE_CURL" not in run_script
    # The wrapper refuses to launch a keyless gateway when the machine's own
    # tmpfs secrets are gone (e.g. wiped by a reboot). The desktop-owned pair is
    # deliberately not part of that gate: without it the extension answers the
    # desktop-owned routes with a clear 503, while third-party calls, which need
    # neither secret, keep working.
    assert "exit 1" in run_script
    assert "awaiting re-provision" in run_script
    assert "desktop_permissions_override ]" not in run_script
    # supervisord keeps it up: autostart + autorestart on crash.
    assert f"[program:{GATEWAY_PROGRAM_NAME}]" in conf
    assert "autostart=true" in conf
    assert "autorestart=true" in conf
    assert "/bin/sh /root/.latchkey/gateway_run.sh" in conf
    # Applied via reread + update + restart so rewritten tmpfs secrets and the
    # wrapper environment take effect even when the supervisord config is unchanged.
    assert _reload_commands(outer) == [
        f"supervisorctl reread && supervisorctl update && "
        f"(supervisorctl restart {GATEWAY_PROGRAM_NAME} || supervisorctl start {GATEWAY_PROGRAM_NAME})"
    ]
    assert all("nohup" not in r.command for r in as_stub(outer).recorded)


def test_ensure_latchkey_gateway_running_writes_secrets_to_0600_tmpfs_files(tmp_path: Path) -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    # Secrets go in a RAM-backed dir under /run, never on the persistent disk
    # beside the encrypted credential store; the wrapper stays on the normal disk.
    key_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_encryption_key")
    password_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_listen_password")
    desktop_password_file = _written_by_path(outer, "/run/mngr-latchkey/desktop_gateway_password")
    desktop_permissions_file = _written_by_path(outer, "/run/mngr-latchkey/desktop_permissions_override")
    run_file = _written_by_path(outer, "/root/.latchkey/gateway_run.sh")
    # Each file's content is the literal secret; none is ever written to a
    # command (see the wrapper test above). The machine's own listen password
    # and the desktop gateway's are separate secrets in separate files.
    assert password_file.content == b"machine-password"
    assert desktop_password_file.content == b"desktop-password"
    assert desktop_permissions_file.content == b"desktop-override-jwt"
    # Secrets are 0600; the wrapper is executable (0700).
    assert key_file.mode == "0600"
    assert password_file.mode == "0600"
    assert desktop_password_file.mode == "0600"
    assert desktop_permissions_file.mode == "0600"
    assert run_file.mode == "0700"
    # The wrapper names every tmpfs secret file it wrote.
    run_script = run_file.content.decode("utf-8")
    assert key_file.path in run_script
    assert password_file.path in run_script
    assert desktop_password_file.path in run_script
    assert desktop_permissions_file.path in run_script


def test_ensure_latchkey_gateway_running_injects_the_machines_own_encryption_key(tmp_path: Path) -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))

    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")

    key_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_encryption_key")
    assert key_file.content == MACHINE_KEY.encode("utf-8")
    # The key never appears in any recorded command string.
    assert all(MACHINE_KEY not in r.command for r in as_stub(outer).recorded)


def test_ensure_latchkey_gateway_running_verifies_secrets_dir_is_ram_backed(tmp_path: Path) -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    # Before writing the key, provisioning creates the /run secrets dir (0700)
    # and asserts its filesystem is RAM-backed (tmpfs/ramfs), refusing to
    # persist the key to disk otherwise.
    guard_commands = [
        r.command
        for r in as_stub(outer).recorded
        if "stat -f -c %T" in r.command and "/run/mngr-latchkey" in r.command
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
    outer = stub_outer(CommandResult(stdout="", stderr="is on a ext4 filesystem", success=False))
    with pytest.raises(RemoteGatewayError, match="RAM-backed secrets directory"):
        _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    # Crucially, no secret file was written when the guard failed.
    assert as_stub(outer).written == []


def _remote_config_text(outer: OuterHostInterface) -> str:
    """Return the raw ~/.latchkey/config.json text written to the VPS."""
    return _written_by_path(outer, "/root/.latchkey/config.json").content.decode("utf-8")


def test_ensure_latchkey_gateway_running_hides_builtin_services_in_config(tmp_path: Path) -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    # The VPS gateway's config.json hides the same confusing built-in services
    # as the desktop gateway, so an agent sees the same set either way.
    config = json.loads(_remote_config_text(outer))
    assert "notion" in config["settings"]["hideBuiltinServices"]


def test_ensure_latchkey_gateway_running_registers_custom_services_in_config(tmp_path: Path) -> None:
    """The VPS gateway is given minds' custom-service registrations.

    The credential sync ships a granted custom service's credentials here, but a
    gateway that does not know the service cannot resolve a request to it, so it
    would never inject them.
    """
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    config = json.loads(_remote_config_text(outer))
    assert config["registeredServices"] == additional_service_registration_entries()
    # Pinned concretely too: comparing the two projections alone would still pass
    # if the bundled catalog degraded to nothing, which is the very failure
    # (a VPS gateway that knows no custom service) this shipping is meant to rule out.
    assert config["registeredServices"]["claude-ai"]["baseApiUrl"] == "https://claude.ai/"


def test_ensure_latchkey_gateway_running_preserves_existing_remote_config(tmp_path: Path) -> None:
    existing = json.dumps({"settings": {"theme": "dark"}, "accounts": {"slack": {}}})
    outer = cast(OuterHostInterface, StubOuter(config_json=existing))
    _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")
    config = json.loads(_remote_config_text(outer))
    # Pre-existing remote config content survives the read-merge-write.
    assert config["settings"]["theme"] == "dark"
    assert config["accounts"] == {"slack": {}}
    assert "notion" in config["settings"]["hideBuiltinServices"]


def test_ensure_latchkey_gateway_running_raises_on_invalid_remote_config(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, StubOuter(config_json="{not json"))
    with pytest.raises(RemoteGatewayError, match=CONFIG_FILENAME):
        _ensure_latchkey_gateway_running(outer, tmp_path, MACHINE_KEY, "machine-password")


def _tunnel_conf(outer: OuterHostInterface) -> str:
    """Return the content of the reverse-tunnel supervisord drop-in written to the VPS."""
    return _written_by_path(outer, f"/etc/supervisor/conf.d/{TUNNEL_PROGRAM_NAME}.conf").content.decode("utf-8")


def test_ensure_latchkey_gateway_reachable_registers_reverse_tunnel_program() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_reachable_from_container(
        outer,
        container_ssh_user="root",
        container_ssh_port=2222,
        container_ssh_key_path=Path("/etc/mngr/container_key"),
        gateway_address=DEFAULT_DOCKER_BRIDGE_ADDRESS,
    )
    conf = _tunnel_conf(outer)
    # A container that predates the outer-host mapping keeps reaching the VPS
    # gateway at its own loopback on the fixed port; the tunnel's far end is the
    # docker bridge address the gateway binds, not the VPS loopback.
    assert f"[program:{TUNNEL_PROGRAM_NAME}]" in conf
    assert "autorestart=true" in conf
    assert f"-R 127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}:{DEFAULT_DOCKER_BRIDGE_ADDRESS}:{OUTER_PORT}" in conf
    assert f":127.0.0.1:{OUTER_PORT}" not in conf
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
        f"supervisorctl reread && supervisorctl update && (supervisorctl start {TUNNEL_PROGRAM_NAME} || true)"
    ]


def test_ensure_latchkey_gateway_reachable_quotes_key_path_with_spaces() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _ensure_latchkey_gateway_reachable_from_container(
        outer,
        container_ssh_user="root",
        container_ssh_port=2222,
        container_ssh_key_path=Path("/tmp/key dir/id_ed25519"),
        gateway_address=DEFAULT_DOCKER_BRIDGE_ADDRESS,
    )
    conf = _tunnel_conf(outer)
    assert "-i '/tmp/key dir/id_ed25519'" in conf


def test_ensure_latchkey_gateway_reachable_raises_on_failure() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="supervisorctl: command not found", success=False))
    with pytest.raises(RemoteGatewayError, match="reload supervisor"):
        _ensure_latchkey_gateway_reachable_from_container(
            outer,
            container_ssh_user="root",
            container_ssh_port=2222,
            container_ssh_key_path=Path("/etc/mngr/container_key"),
            gateway_address=DEFAULT_DOCKER_BRIDGE_ADDRESS,
        )


def test_resolve_bridge_listen_host_is_the_docker_bridge_address() -> None:
    outer = cast(OuterHostInterface, StubOuter(docker_bridge_address="172.18.0.1"))
    assert _resolve_bridge_listen_host(outer) == "172.18.0.1"


def test_resolve_bridge_listen_host_refuses_a_machine_with_no_docker_bridge() -> None:
    # Failing closed: a wildcard bind on a VPS would put the gateway on its
    # public interface, password-gated only.
    outer = cast(OuterHostInterface, StubOuter(docker_bridge_address=""))
    with pytest.raises(RemoteGatewayError, match="wildcard"):
        _resolve_bridge_listen_host(outer)


def test_ensure_bridge_services_firewalled_refuses_a_machine_the_firewall_cannot_be_applied_on() -> None:
    # Failing closed: without the policy the bridge-bound services would answer
    # a packet for the bridge address arriving on the VPS's public interface.
    outer = stub_outer(CommandResult(stdout="", stderr="E: Unable to locate package nftables", success=False))
    with pytest.raises(RemoteGatewayError, match="without the firewall.*Unable to locate package nftables"):
        _ensure_bridge_services_firewalled(outer)
    assert as_stub(outer).written == []


def test_does_container_resolve_outer_host_reads_the_creation_time_mapping() -> None:
    assert _does_container_resolve_outer_host(
        cast(OuterHostInterface, StubOuter(container_extra_hosts=("host.docker.internal:host-gateway",))), "mngr-ws"
    )
    # An older container has no extra hosts at all (docker reports ``null``).
    assert not _does_container_resolve_outer_host(
        cast(OuterHostInterface, StubOuter(container_extra_hosts=())), "mngr-ws"
    )
    # Only the outer-host name counts; some unrelated mapping does not.
    assert not _does_container_resolve_outer_host(
        cast(OuterHostInterface, StubOuter(container_extra_hosts=("registry.internal:10.0.0.7",))), "mngr-ws"
    )


def test_does_container_resolve_outer_host_inspects_the_named_container() -> None:
    outer = cast(OuterHostInterface, StubOuter())
    _does_container_resolve_outer_host(outer, "mngr-ws-7")
    command = as_stub(outer).recorded[-1].command
    assert command.startswith("docker inspect")
    assert "mngr-ws-7" in command
    assert "ExtraHosts" in command


def test_does_container_resolve_outer_host_raises_when_the_container_cannot_be_inspected() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="Error: No such object: mngr-ws", success=False))
    with pytest.raises(RemoteGatewayError, match="No such object"):
        _does_container_resolve_outer_host(outer, "mngr-ws")


def _registered_tunnel_files() -> dict[str, bytes]:
    """The supervisord drop-in an earlier provisioning of the same VPS left registered."""
    return {f"/etc/supervisor/conf.d/{TUNNEL_PROGRAM_NAME}.conf": b"[program:latchkey-tunnel]\n"}


def test_does_container_need_reverse_tunnel_when_the_container_cannot_resolve_the_outer_host() -> None:
    outer = cast(OuterHostInterface, StubOuter(container_extra_hosts=()))
    assert _does_container_need_reverse_tunnel(outer, "mngr-ws")


def test_does_container_need_reverse_tunnel_when_one_is_already_registered() -> None:
    # The agent's LATCHKEY_GATEWAY was decided by the client that created its
    # host: an older client names the loopback even in a container that carries
    # the mapping, and registered the tunnel for it. The tunnel is kept.
    outer = cast(OuterHostInterface, StubOuter(remote_files=_registered_tunnel_files()))
    assert _does_container_need_reverse_tunnel(outer, "mngr-ws")


def test_does_container_need_reverse_tunnel_is_false_for_a_container_that_resolves_the_outer_host() -> None:
    outer = cast(OuterHostInterface, StubOuter())
    assert not _does_container_need_reverse_tunnel(outer, "mngr-ws")
    # The decision reads the container's creation flags, and nothing else is
    # run on the VPS for it.
    assert [r.command.split(" ", 2)[:2] for r in as_stub(outer).recorded] == [["docker", "inspect"]]


def test_ensure_container_tunnel_keypair_generates_key_and_authorizes_in_container() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    key_path = _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="root")
    # Private key lands under the resolved remote latchkey dir.
    assert key_path == Path("/root/.latchkey/container_tunnel_key")
    script = as_stub(outer).recorded[-1].command
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
    outer = cast(OuterHostInterface, StubOuter(home="/home/agent"))
    key_path = _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="agent")
    assert key_path == Path("/home/agent/.latchkey/container_tunnel_key")
    assert "docker exec -u agent" in as_stub(outer).recorded[-1].command


def test_ensure_container_tunnel_keypair_raises_on_failure() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="Error: No such container: mngr-ws", success=False))
    with pytest.raises(RemoteGatewayError, match="No such container"):
        _ensure_container_tunnel_keypair(outer, container_name="mngr-ws", container_ssh_user="root")


def test_provision_remote_gateway_runs_full_sequence_on_the_outer_host(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, StubOuter(container_name="mngr-ws-1"))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        desktop_secrets=_DESKTOP_SECRETS,
    )
    commands = _recorded_commands_text(outer)
    written = _written_content_text(outer)
    # Install latchkey + supervisor, resolve the docker bridge address, fence
    # the bridge-bound ports before either service starts, register the gateway
    # supervisord program bound there, and find + inspect the container. It
    # carries the outer-host mapping and no tunnel was ever registered on this
    # VPS, so no tunnel is wired: no keypair is minted and nothing is exec'd
    # into the container.
    assert "npm install -g latchkey@" in commands
    assert "apt-get install -y supervisor" in commands
    # One round trip resolves the bridge address for the owner-exec daemon and
    # the gateway alike.
    assert commands.count("addr show docker0") == 1
    assert "apt-get install -y nftables" in commands
    assert f"tcp dport {{ {OUTER_PORT}, {VM_EXEC_PORT} }} counter drop" in written
    assert commands.index(f"systemctl enable --now {BRIDGE_SERVICES_FIREWALL_UNIT_NAME}") < commands.index(
        "owner-exec"
    )
    assert "docker ps -a --filter" in commands
    assert "com.imbue.mngr.host-id=" in commands
    assert "docker inspect" in commands
    assert "mngr-ws-1" in commands
    assert "ssh-keygen" not in commands
    assert "docker exec" not in commands
    # A legacy (nohup + PID-file) gateway/tunnel is torn down *before* the new
    # supervisord program is applied, so it frees OUTER_PORT first.
    assert '"$HOME/.latchkey/gateway.pid"' in commands
    assert commands.index('"$HOME/.latchkey/gateway.pid"') < commands.index("supervisorctl reread")
    # Only the gateway supervisord program was written, listening on the bridge.
    assert f"[program:{GATEWAY_PROGRAM_NAME}]" in written
    assert f"[program:{TUNNEL_PROGRAM_NAME}]" not in written
    assert "exec latchkey gateway" in written
    assert f"export LATCHKEY_GATEWAY_LISTEN_HOST={DEFAULT_DOCKER_BRIDGE_ADDRESS}" in written
    # The VPS gateway's config.json hides the confusing built-in services and
    # loads only the desktop-gateway forwarding extension.
    assert "hideBuiltinServices" in written and "notion" in written
    assert "Desktop latchkey gateway is unreachable" in written
    assert "-R 127.0.0.1:" not in written
    # Every password is written to a file, never a command. This machine is
    # brand new, so it takes this computer's password as its own listen
    # password, which is also what the forwarding extension presents back here.
    assert "desktop-password" not in commands
    assert [w.path for w in as_stub(outer).written if w.content == b"desktop-password"] == [
        "/run/mngr-latchkey/gateway_listen_password",
        "/run/mngr-latchkey/desktop_gateway_password",
    ]


def test_provision_remote_gateway_keeps_the_reverse_tunnel_for_a_container_without_the_mapping(
    tmp_path: Path,
) -> None:
    """A container created before the outer-host mapping still reaches the gateway at its own loopback.

    Its ``LATCHKEY_GATEWAY`` names ``127.0.0.1`` and neither that nor the
    missing ``--add-host`` can change for the life of the container, so the
    reverse tunnel is kept for it -- pointed at the bridge address the gateway
    binds.
    """
    outer = cast(OuterHostInterface, StubOuter(container_name="mngr-ws-old", container_extra_hosts=()))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        desktop_secrets=_DESKTOP_SECRETS,
    )
    commands = _recorded_commands_text(outer)
    written = _written_content_text(outer)
    # The gateway itself is wired exactly as for a new container.
    assert f"export LATCHKEY_GATEWAY_LISTEN_HOST={DEFAULT_DOCKER_BRIDGE_ADDRESS}" in written
    # Then the keypair is minted and authorized in this container, and the
    # tunnel program registered with the gateway's bridge address as its far end.
    assert "ssh-keygen -t ed25519" in commands
    assert "docker exec -u root" in commands
    assert "mngr-ws-old" in commands
    assert f"[program:{TUNNEL_PROGRAM_NAME}]" in written
    assert f"-R 127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}:{DEFAULT_DOCKER_BRIDGE_ADDRESS}:{OUTER_PORT}" in written


def test_provision_remote_gateway_keeps_a_registered_reverse_tunnel_for_a_container_with_the_mapping(
    tmp_path: Path,
) -> None:
    """A tunnel an older client registered is kept and re-pointed at the address the gateway binds.

    The container carries the mapping, but its agent's ``LATCHKEY_GATEWAY`` was
    decided by the client that created its host, which may name the loopback;
    the tunnel that client registered (forwarding to the VPS loopback the
    gateway no longer binds) is what says so, and it is never dropped.
    """
    outer = cast(OuterHostInterface, StubOuter(container_name="mngr-ws-kept", remote_files=_registered_tunnel_files()))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        desktop_secrets=_DESKTOP_SECRETS,
    )
    commands = _recorded_commands_text(outer)
    written = _written_content_text(outer)
    assert "docker inspect" in commands
    assert "ssh-keygen -t ed25519" in commands
    assert "docker exec -u root" in commands
    assert "mngr-ws-kept" in commands
    assert f"[program:{TUNNEL_PROGRAM_NAME}]" in written
    assert f"-R 127.0.0.1:{AGENT_SIDE_LATCHKEY_PORT}:{DEFAULT_DOCKER_BRIDGE_ADDRESS}:{OUTER_PORT}" in written
    assert f":127.0.0.1:{OUTER_PORT}" not in written


def test_migrate_legacy_remote_gateway_state_kills_pidfile_processes_and_scrubs_secrets() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="", success=True))
    _migrate_legacy_remote_gateway_state(outer)
    assert len(as_stub(outer).recorded) == 1
    script = as_stub(outer).recorded[0].command
    # Kills the legacy nohup gateway + tunnel by their PID files, each guarded by
    # a /proc cmdline marker so a reused PID is never signalled (TERM then KILL
    # so the held port/forward is freed before the supervisord replacement).
    assert '"$HOME/.latchkey/gateway.pid"' in script
    assert '"$HOME/.latchkey/tunnel.pid"' in script
    assert "grep -qaF gateway " in script
    assert f"grep -qaF 127.0.0.1:1990:127.0.0.1:{OUTER_PORT} " in script
    assert 'kill "$_pid"' in script
    assert 'kill -9 "$_pid"' in script
    assert 'rm -f "$_pidfile"' in script
    # Scrubs any on-disk secrets an intermediate build persisted (now tmpfs-only);
    # double-quoted so $HOME expands (shlex.quote would stop it).
    assert (
        'rm -f "$HOME/.latchkey/gateway_encryption_key" "$HOME/.latchkey/gateway_listen_password" '
        '"$HOME/.latchkey/desktop_permissions_override"'
    ) in script


def test_migrate_legacy_remote_gateway_state_raises_on_failure() -> None:
    outer = stub_outer(CommandResult(stdout="", stderr="boom", success=False))
    with pytest.raises(RemoteGatewayError, match="migrate legacy latchkey gateway"):
        _migrate_legacy_remote_gateway_state(outer)


def test_provision_remote_gateway_raises_when_container_not_found(tmp_path: Path) -> None:
    outer = cast(OuterHostInterface, StubOuter(container_name=""))
    with pytest.raises(RemoteGatewayError, match="No container labeled"):
        provision_remote_gateway(
            outer,
            host_id=HostId(),
            container_ssh_user="root",
            container_ssh_port=2222,
            latchkey_directory=tmp_path,
            desktop_secrets=_DESKTOP_SECRETS,
        )


def test_provision_remote_gateway_is_noop_on_local_outer_host(tmp_path: Path) -> None:
    # A local outer (e.g. the local docker daemon's machine) must never be
    # provisioned -- we don't apt/npm-install latchkey on the user's computer.
    outer = cast(OuterHostInterface, StubOuter(is_local=True))
    provision_remote_gateway(
        outer,
        host_id=HostId(),
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        desktop_secrets=_DESKTOP_SECRETS,
    )
    assert as_stub(outer).recorded == []


def _outer_with_preexisting_latchkey_dir(is_present: bool) -> OuterHostInterface:
    return cast(
        OuterHostInterface,
        StubOuter(
            result=CommandResult(stdout="", stderr="", success=True),
            is_remote_latchkey_dir_present=is_present,
        ),
    )


def _outer_with_machine_store(machine_store_key: str, machine_accounts: dict[str, list[str]]) -> OuterHostInterface:
    """A provisioned machine already holding a credential store, readable only under its own key."""
    return cast(
        OuterHostInterface,
        StubOuter(
            result=CommandResult(stdout="", stderr="", success=True),
            is_remote_latchkey_dir_present=True,
            machine_accounts=machine_accounts,
            machine_store_key=machine_store_key,
        ),
    )


def test_resolve_machine_encryption_key_mints_a_key_of_its_own_for_a_fresh_machine(tmp_path: Path) -> None:
    """A machine's credentials are readable by that machine and the desktops managing it, not by every VPS."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()

    key = _resolve_machine_encryption_key(_outer_with_preexisting_latchkey_dir(False), latchkey_directory, host_id)

    assert key.get_secret_value() != load_or_create_encryption_key(latchkey_directory).get_secret_value()
    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) == key


def test_resolve_machine_encryption_key_is_decided_once_and_then_read_back(tmp_path: Path) -> None:
    """Rotating a live machine's key would make the store it already holds unreadable."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    first = _resolve_machine_encryption_key(_outer_with_preexisting_latchkey_dir(False), latchkey_directory, host_id)

    # Even reading as though the machine were now provisioned by an older build.
    second = _resolve_machine_encryption_key(_outer_with_preexisting_latchkey_dir(True), latchkey_directory, host_id)

    assert second == first


def test_resolve_machine_encryption_key_adopts_the_key_the_machine_is_running_under(tmp_path: Path) -> None:
    """Another install may have provisioned the machine; its running key is adopted, never replaced.

    Minting (or guessing) a key here instead would re-key the machine out from
    under the install that provisioned it, breaking the store both installs
    are supposed to share.
    """
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    outer = _outer_with_preexisting_latchkey_dir(True)
    as_stub(outer).remote_files["/run/mngr-latchkey/gateway_encryption_key"] = b"other-installs-key-7841\n"

    key = _resolve_machine_encryption_key(outer, latchkey_directory, host_id)

    assert key.get_secret_value() == "other-installs-key-7841"
    # Recorded durably, so this install can hand the key back after a reboot too.
    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) == key


def test_resolve_machine_encryption_key_corrects_a_record_the_machine_disagrees_with(tmp_path: Path) -> None:
    """The machine's running key is the truth; the record here is only a mirror of it."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    data_dir = plugin_data_dir(latchkey_directory)
    store_machine_encryption_key(data_dir, host_id, SecretStr("stale-record-1189"))
    outer = _outer_with_preexisting_latchkey_dir(True)
    as_stub(outer).remote_files["/run/mngr-latchkey/gateway_encryption_key"] = b"machines-own-key-2258"

    key = _resolve_machine_encryption_key(outer, latchkey_directory, host_id)

    assert key.get_secret_value() == "machines-own-key-2258"
    assert stored_machine_encryption_key(data_dir, host_id) == key


def test_resolve_machine_encryption_key_verifies_the_desktop_key_against_a_rebooted_legacy_store(
    tmp_path: Path,
) -> None:
    """A store the desktop key actually opens is our own legacy machine's, so the key is kept."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    desktop_key = load_or_create_encryption_key(latchkey_directory)
    outer = _outer_with_machine_store(desktop_key.get_secret_value(), {"slack": ["a@example.com"]})

    key = _resolve_machine_encryption_key(outer, latchkey_directory, host_id)

    assert key == desktop_key
    # The store opened, so nothing was abandoned.
    assert as_stub(outer).machine_accounts == {"slack": ["a@example.com"]}


def test_a_candidate_key_is_probed_without_its_script_reaching_the_logs(tmp_path: Path) -> None:
    """The probe carries the candidate key in the script body, so it is withheld like any other."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    desktop_key = load_or_create_encryption_key(latchkey_directory)
    outer = _outer_with_machine_store(desktop_key.get_secret_value(), {"slack": ["a@example.com"]})

    assert _does_key_open_the_machine_store(outer, desktop_key)

    probes = [entry for entry in as_stub(outer).recorded if "auth list --offline" in entry.command]
    assert len(probes) == 1
    assert probes[0].is_kept_out_of_logs


def test_resolve_machine_encryption_key_abandons_a_store_nobody_present_can_read(tmp_path: Path) -> None:
    """Provisioned by a computer that is gone, rebooted since: signing in again is possible, waiting is not."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    outer = _outer_with_machine_store("a-key-only-the-lost-computer-held", {"slack": ["lost@example.com"]})

    key = _resolve_machine_encryption_key(outer, latchkey_directory, host_id)

    # A fresh key of the machine's own -- neither the unreadable store's nor the desktop's.
    assert key.get_secret_value() != "a-key-only-the-lost-computer-held"
    assert key.get_secret_value() != load_or_create_encryption_key(latchkey_directory).get_secret_value()
    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) == key
    # The unreadable store is gone rather than left to break the freshly-keyed gateway.
    assert as_stub(outer).machine_accounts == {}
    assert any(
        command.startswith("rm -f ") and "credentials.json.enc" in command
        for command in as_stub(outer).recorded_commands()
    )


def test_resolve_machine_encryption_key_keeps_the_desktop_key_for_an_older_builds_machine(tmp_path: Path) -> None:
    """Agents created before the one-gateway rollout carry a JWT the desktop key signed.

    The gateway derives the key it validates those with from its encryption key,
    so rotating it would reject every request such an agent makes -- and its
    token was baked into its environment at creation, with no way to reissue it.
    """
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()

    key = _resolve_machine_encryption_key(_outer_with_preexisting_latchkey_dir(True), latchkey_directory, host_id)

    assert key == load_or_create_encryption_key(latchkey_directory)


def test_provisioning_hands_the_gateway_the_machines_own_key(tmp_path: Path) -> None:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    outer = _outer_with_preexisting_latchkey_dir(False)

    provision_remote_gateway(
        outer,
        host_id=host_id,
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=latchkey_directory,
        desktop_secrets=_DESKTOP_SECRETS,
    )

    stored = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert stored is not None
    key_file = _written_by_path(outer, "/run/mngr-latchkey/gateway_encryption_key")
    assert key_file.content == stored.get_secret_value().encode("utf-8")
    assert key_file.content != load_or_create_encryption_key(latchkey_directory).get_secret_value().encode("utf-8")


# -- the machine's own gateway listen password ----------------------------------


def test_resolve_machine_gateway_password_seeds_a_machine_nobody_has_provisioned_yet(tmp_path: Path) -> None:
    """A brand-new machine takes this computer's password: it is what its workspaces present."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()

    password = _resolve_machine_gateway_password(
        _outer_with_preexisting_latchkey_dir(False), latchkey_directory, host_id, "this-computers-password"
    )

    assert password == "this-computers-password"
    # Recorded durably, so a reboot that wipes the machine's copy does not lose it.
    assert stored_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id) == password


def test_resolve_machine_gateway_password_adopts_the_password_the_machine_is_running_under(tmp_path: Path) -> None:
    """Another of the user's computers created this machine's workspaces; their env file is fixed.

    Writing this computer's own password here would answer every request those
    workspaces make with a 401, with no way to tell them the new value.
    """
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    outer = _outer_with_preexisting_latchkey_dir(True)
    as_stub(outer).remote_files["/run/mngr-latchkey/gateway_listen_password"] = b"other-computers-password\n"

    password = _resolve_machine_gateway_password(outer, latchkey_directory, host_id, "this-computers-password")

    assert password == "other-computers-password"
    assert stored_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id) == password


def test_resolve_machine_gateway_password_hands_a_rebooted_machine_its_recorded_password(tmp_path: Path) -> None:
    """A reboot wipes the machine's tmpfs copy; the record here is what puts it back."""
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    store_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id, "the-password-it-was-created-with")

    password = _resolve_machine_gateway_password(
        _outer_with_preexisting_latchkey_dir(True), latchkey_directory, host_id, "this-computers-password"
    )

    assert password == "the-password-it-was-created-with"


def test_provisioning_keeps_the_machines_password_while_replacing_the_desktops(tmp_path: Path) -> None:
    """Moving to another computer must not re-key the gateway its workspaces authenticate to.

    The whole point of the split: the machine keeps the listen password its
    workspaces were created with, while the secrets for the hop back to the
    user's computer become this computer's.
    """
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    host_id = HostId.generate()
    outer = _outer_with_preexisting_latchkey_dir(True)
    as_stub(outer).remote_files["/run/mngr-latchkey/gateway_listen_password"] = b"other-computers-password"

    provision_remote_gateway(
        outer,
        host_id=host_id,
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=latchkey_directory,
        desktop_secrets=_DESKTOP_SECRETS,
    )

    assert _written_by_path(outer, "/run/mngr-latchkey/gateway_listen_password").content == b"other-computers-password"
    assert _written_by_path(outer, "/run/mngr-latchkey/desktop_gateway_password").content == b"desktop-password"
    assert (
        _written_by_path(outer, "/run/mngr-latchkey/desktop_permissions_override").content == b"desktop-override-jwt"
    )


# -- permission reconciliation --------------------------------------------------


def _machine_with_permissions(policy: str) -> OuterHostInterface:
    return cast(
        OuterHostInterface,
        StubOuter(
            result=CommandResult(stdout="", stderr="", success=True),
            machine_permissions=policy,
        ),
    )


def _write_local_permissions(latchkey_directory: Path, host_id: HostId, policy: str) -> Path:
    path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy)
    return path


_GRANTED_HERE = '{"rules": [{"slack-api": ["slack-read-all"]}]}'
_GRANTED_ELSEWHERE = '{"rules": [{"github-rest-api": ["github-read-all"]}]}'


def test_sync_permissions_adopts_the_policy_the_machine_holds(tmp_path: Path) -> None:
    """The machine's file is the rendezvous: it is how a second computer sees what the first granted.

    A local edit is deliberately *not* pushed by this pass -- it is pushed when
    it is made -- so a machine that already has a policy is only ever adopted
    from here, never written over by a stale local copy.
    """
    host_id = HostId.generate()
    latchkey_directory = tmp_path / "latchkey"
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    outer = _machine_with_permissions(_GRANTED_ELSEWHERE)

    sync_permissions(outer, latchkey_directory, host_id, _REMOTE_DIR)

    assert local_path.read_text() == _GRANTED_ELSEWHERE
    # Nothing was written back: the machine already holds what it holds.
    assert [write.path for write in as_stub(outer).written] == []


def test_sync_permissions_leaves_an_unchanged_local_copy_untouched(tmp_path: Path) -> None:
    """Adopting an identical policy must not rewrite the local file (nothing to say, nothing to churn)."""
    host_id = HostId.generate()
    latchkey_directory = tmp_path / "latchkey"
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    modified_at_before = local_path.stat().st_mtime_ns
    outer = _machine_with_permissions(_GRANTED_HERE)

    sync_permissions(outer, latchkey_directory, host_id, _REMOTE_DIR)

    assert local_path.stat().st_mtime_ns == modified_at_before


def test_sync_permissions_refuses_a_policy_it_cannot_read_rather_than_storing_it(tmp_path: Path) -> None:
    """A policy this build cannot parse must not replace one it can enforce."""
    host_id = HostId.generate()
    latchkey_directory = tmp_path / "latchkey"
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    outer = _machine_with_permissions('{"rules": "not-a-list"}')

    with pytest.raises(RemoteGatewayError, match="cannot read"):
        sync_permissions(outer, latchkey_directory, host_id, _REMOTE_DIR)

    assert local_path.read_text() == _GRANTED_HERE


def test_machine_latchkey_layout_names_exactly_what_provisioning_writes(tmp_path: Path) -> None:
    # The migrate carries a machine's latchkey state by these tuples, so a file
    # provisioning starts writing (or stops writing) without them changing
    # would be silently dropped (or read as absent) on every migration. The
    # container predates the outer-host mapping, so the reverse tunnel (the
    # one file a container with the mapping does without) is written too.
    outer = cast(OuterHostInterface, StubOuter(container_name="mngr-ws-1", container_extra_hosts=()))
    host_id = HostId()
    provision_remote_gateway(
        outer,
        host_id=host_id,
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey_directory=tmp_path,
        desktop_secrets=_DESKTOP_SECRETS,
    )
    # The policy is seeded by the reconcile's permissions pass, not the gateway pass.
    sync_permissions(outer, tmp_path, host_id, _REMOTE_DIR)
    written_paths = {Path(w.path) for w in as_stub(outer).written}
    written_disk_names = {
        path.name
        for path in written_paths
        if path.parent == _REMOTE_DIR and not path.name.endswith(".log") and not path.name.endswith(".candidate")
    }
    # The credential store and its format stamp arrive with the first credential
    # handover and the tunnel key pair is minted by ssh-keygen on the machine;
    # everything else under ~/.latchkey is written by this pass.
    assert written_disk_names == set(MACHINE_LATCHKEY_DISK_FILENAMES) - {
        CREDENTIALS_STORE_FILENAME,
        UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        CONTAINER_TUNNEL_KEY_FILENAME,
        f"{CONTAINER_TUNNEL_KEY_FILENAME}.pub",
    }
    assert {path.name for path in written_paths if path.parent == TMPFS_SECRETS_DIR} == set(
        MACHINE_LATCHKEY_TMPFS_FILENAMES
    )
    assert {path.name for path in written_paths if path.parent == SUPERVISOR_CONFD_DIR} == set(
        MACHINE_LATCHKEY_SUPERVISOR_CONF_FILENAMES
    )
    # The wrapper refuses to start without exactly the machine's own pair.
    run_script = _written_by_path(outer, str(_REMOTE_DIR / GATEWAY_RUN_SCRIPT_FILENAME)).content.decode("utf-8")
    refusal_line = next(line for line in run_script.splitlines() if line.startswith("if [ ! -s "))
    for filename in MACHINE_LATCHKEY_GATEWAY_REQUIRED_TMPFS_FILENAMES:
        assert str(TMPFS_SECRETS_DIR / filename) in refusal_line
    assert refusal_line.count("! -s") == len(MACHINE_LATCHKEY_GATEWAY_REQUIRED_TMPFS_FILENAMES)


def _staged_extension_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Lay out an empty extensions directory and return it with its destination and staged paths."""
    extensions_dir = tmp_path / REMOTE_EXTENSIONS_DIR_NAME
    extensions_dir.mkdir()
    destination = extensions_dir / REMOTE_GATEWAY_EXTENSION_FILENAME
    candidate = destination.with_name(destination.name + _REMOTE_EXTENSION_CANDIDATE_SUFFIX)
    return extensions_dir, destination, candidate


def test_extension_install_script_installs_the_staged_extension(local_outer_host: OuterHost, tmp_path: Path) -> None:
    """The staged extension is moved into place with the restrictive remote file mode."""
    extensions_dir, destination, candidate = _staged_extension_paths(tmp_path)
    candidate.write_text("export default 'staged';\n")

    result = local_outer_host.execute_idempotent_command(
        _build_extension_install_script(extensions_dir, candidate, destination)
    )

    assert result.success
    assert destination.read_text() == "export default 'staged';\n"
    assert not candidate.exists()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_replaying_the_extension_install_succeeds_after_it_already_installed(
    local_outer_host: OuterHost, tmp_path: Path
) -> None:
    """The retry that a lost SSH response triggers must not fail an install that already landed.

    ``execute_idempotent_command`` re-runs its command after a transient SSH
    error, including when the far side had in fact completed it, so running the
    install twice stands in for that replay.
    """
    extensions_dir, destination, candidate = _staged_extension_paths(tmp_path)
    candidate.write_text("export default 'staged';\n")
    script = _build_extension_install_script(extensions_dir, candidate, destination)

    assert local_outer_host.execute_idempotent_command(script).success
    replayed = local_outer_host.execute_idempotent_command(script)

    assert replayed.success
    assert destination.read_text() == "export default 'staged';\n"


def test_replaying_the_extension_install_succeeds_after_it_discarded_an_identical_candidate(
    local_outer_host: OuterHost, tmp_path: Path
) -> None:
    """A replay of the up-to-date branch is a no-op too, and leaves the installed copy alone."""
    extensions_dir, destination, candidate = _staged_extension_paths(tmp_path)
    destination.write_text("export default 'installed';\n")
    candidate.write_text("export default 'installed';\n")
    script = _build_extension_install_script(extensions_dir, candidate, destination)

    assert local_outer_host.execute_idempotent_command(script).success
    assert not candidate.exists()
    replayed = local_outer_host.execute_idempotent_command(script)

    assert replayed.success
    assert destination.read_text() == "export default 'installed';\n"


def test_extension_install_script_fails_when_the_candidate_and_destination_are_both_gone(
    local_outer_host: OuterHost, tmp_path: Path
) -> None:
    """A staged extension that vanished without landing is reported, not passed off as a success."""
    extensions_dir, destination, candidate = _staged_extension_paths(tmp_path)

    result = local_outer_host.execute_idempotent_command(
        _build_extension_install_script(extensions_dir, candidate, destination)
    )

    assert not result.success
    assert str(destination) in result.stderr
    assert not destination.exists()
