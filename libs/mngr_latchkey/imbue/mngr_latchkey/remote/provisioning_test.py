import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.additional_services import additional_service_registration_entries
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import REMOTE_GATEWAY_EXTENSION_FILENAME
from imbue.mngr_latchkey.core import bundled_gateway_extension_content
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote._machine import DESKTOP_GATEWAY_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import DESKTOP_PERMISSIONS_OVERRIDE_FILENAME
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import GATEWAY_LISTEN_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import REMOTE_COMMAND_NAME
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_gateway_password
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import stored_machine_gateway_password
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.mock_outer_host_test import DEFAULT_DOCKER_BRIDGE_ADDRESS
from imbue.mngr_latchkey.remote.mock_outer_host_test import FakeVps
from imbue.mngr_latchkey.remote.mock_outer_host_test import OUTER_HOST_EXTRA_HOST
from imbue.mngr_latchkey.remote.mock_outer_host_test import as_vps
from imbue.mngr_latchkey.remote.mock_outer_host_test import fake_latchkey_binary
from imbue.mngr_latchkey.remote.mock_outer_host_test import fake_vps
from imbue.mngr_latchkey.remote.package import CONTAINER_TUNNEL_KEY_FILENAME
from imbue.mngr_latchkey.remote.package import GATEWAY_CONF_FILENAME
from imbue.mngr_latchkey.remote.package import GATEWAY_PROGRAM_NAME
from imbue.mngr_latchkey.remote.package import REMOTE_EXTENSIONS_DIR_NAME
from imbue.mngr_latchkey.remote.package import TUNNEL_PROGRAM_NAME
from imbue.mngr_latchkey.remote.provisioning import DesktopGatewaySecrets
from imbue.mngr_latchkey.remote.provisioning import _do_extra_hosts_resolve_outer_host
from imbue.mngr_latchkey.remote.provisioning import _does_container_need_reverse_tunnel
from imbue.mngr_latchkey.remote.provisioning import provision_remote_gateway
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# This computer's own gateway secrets, as every provisioning call here hands
# them over. Distinct strings from the machine's own listen password so a test
# can tell which secret landed where.
_DESKTOP_SECRETS = DesktopGatewaySecrets(
    gateway_password="desktop-password",
    permissions_override="desktop-override-jwt",
)

_GRANTED_HERE = '{"rules": [{"slack-api": ["slack-read-all"]}]}'
_GRANTED_ELSEWHERE = '{"rules": [{"github-rest-api": ["github-read-all"]}]}'


def _desktop_latchkey(latchkey_directory: Path) -> Latchkey:
    """This computer's latchkey, whose CLI is the fake one that tries a key against a store as the real one does."""
    return Latchkey(
        latchkey_directory=latchkey_directory,
        latchkey_binary=str(fake_latchkey_binary(latchkey_directory.parent)),
    )


def _provision(outer: OuterHostInterface, latchkey_directory: Path, host_id: HostId) -> None:
    provision_remote_gateway(
        outer,
        host_id=host_id,
        container_ssh_user="root",
        container_ssh_port=2222,
        latchkey=_desktop_latchkey(latchkey_directory),
        desktop_secrets=_DESKTOP_SECRETS,
        package_layout=as_vps(outer).layout,
    )


def _latchkey_directory(tmp_path: Path) -> Path:
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    return latchkey_directory


def _write_local_permissions(latchkey_directory: Path, host_id: HostId, policy: str) -> Path:
    path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(policy)
    return path


def _latchkey_commands(vps: FakeVps) -> list[str]:
    """The remote commands that concern the latchkey gateway (the owner-exec daemon's and the bridge probe are not under test)."""
    return [command for command in vps.recorded_commands() if "owner-exec" not in command and "docker0" not in command]


# The whole pass.


def test_provision_remote_gateway_installs_the_package_and_wires_a_fresh_machine(tmp_path: Path) -> None:
    """Five remote operations stand a fresh machine up: the bridge probe, an upload, the install, a read and an apply.

    The container carries the outer-host mapping and no tunnel was ever wired
    into it, so the gateway is all that is wired: no keypair is minted, nothing
    is exec'd into the container, and the tunnel program is never started.
    """
    outer = fake_vps(tmp_path, container_name="mngr-ws-1")
    vps = as_vps(outer)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    # The package went up as one artifact, and the bootstrap installed it.
    uploaded = [entry for entry in vps.written if entry.path.endswith(".deb")]
    assert len(uploaded) == 1
    assert uploaded[0].path.startswith(str(vps.layout.artifact_dir))
    # Renamed into place: a file planted at the predictable path in /tmp
    # cannot be what dpkg reads.
    assert uploaded[0].is_atomic is True
    assert len(vps.installed_versions) == 1
    # One round trip resolves the bridge address for the owner-exec daemon and
    # the gateway alike.
    assert len([command for command in vps.recorded_commands() if "addr show docker0" in command]) == 1
    commands = _latchkey_commands(vps)
    assert len(commands) == 3
    assert "dpkg -i" in commands[0]
    assert commands[1].startswith(f"{REMOTE_COMMAND_NAME} read-state")
    assert commands[2].startswith(f"{REMOTE_COMMAND_NAME} apply-state")
    # The machine's own secrets and the desktop-owned pair landed in RAM, owner-only.
    recorded_key = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert recorded_key is not None
    assert vps.secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == recorded_key.get_secret_value()
    assert vps.secret(GATEWAY_LISTEN_PASSWORD_FILENAME) == "desktop-password"
    assert vps.secret(DESKTOP_GATEWAY_PASSWORD_FILENAME) == "desktop-password"
    assert vps.secret(DESKTOP_PERMISSIONS_OVERRIDE_FILENAME) == "desktop-override-jwt"
    assert all(vps.secret_mode(name) == 0o600 for name in vps.secrets_dir_entries())
    # No secret ever traveled as a file write or in a bare command.
    assert [entry.path for entry in vps.written if b"desktop-password" in entry.content] == []
    assert all("desktop-password" not in command for command in commands)
    # The gateway binds the bridge address; the container was found by its
    # label and inspected for how it was created, and that is all that touched
    # it: no keypair, no tunnel target, only the gateway (re)started.
    assert vps.gateway_conf() == f"LK_GATEWAY_LISTEN_HOST='{DEFAULT_DOCKER_BRIDGE_ADDRESS}'\n"
    assert vps.docker_calls() == [
        f"ps -a --filter label=com.imbue.mngr.host-id={host_id} --format {{{{.Names}}}}",
        "inspect -f {{json .HostConfig.ExtraHosts}} mngr-ws-1",
    ]
    assert vps.tunnel_key() is None
    assert vps.tunnel_conf() is None
    assert vps.container_authorized_keys("mngr-ws-1", "root") == ""
    assert vps.supervisorctl_calls() == [f"restart {GATEWAY_PROGRAM_NAME}"]
    # The gateway's config hides the confusing built-in services and registers
    # minds' custom ones; the policy is the restrictive default.
    config = json.loads(vps.machine_config() or "{}")
    assert config["settings"]["hideBuiltinServices"] == ["notion"]
    assert set(config["registeredServices"]) == set(additional_service_registration_entries())
    assert vps.machine_permissions() == '{\n  "rules": []\n}'
    # The gateway's extension is the package's copy, where the gateway loads it from.
    shipped_extension = vps.latchkey_dir / REMOTE_EXTENSIONS_DIR_NAME / REMOTE_GATEWAY_EXTENSION_FILENAME
    assert shipped_extension.read_text() == bundled_gateway_extension_content(REMOTE_GATEWAY_EXTENSION_FILENAME)
    assert vps.latchkey_dir_entries() == [
        CONFIG_FILENAME,
        REMOTE_EXTENSIONS_DIR_NAME,
        GATEWAY_CONF_FILENAME,
        PERMISSIONS_CONFIG_FILENAME,
    ]
    # The apply left no scratch behind in RAM.
    assert vps.secrets_dir_entries() == [
        DESKTOP_GATEWAY_PASSWORD_FILENAME,
        DESKTOP_PERMISSIONS_OVERRIDE_FILENAME,
        GATEWAY_ENCRYPTION_KEY_FILENAME,
        GATEWAY_LISTEN_PASSWORD_FILENAME,
    ]


def test_provisioning_again_adopts_what_the_machine_runs_under_and_bounces_only_the_gateway(
    tmp_path: Path,
) -> None:
    """A second pass changes nothing the machine already has, and only the gateway is restarted for its secrets."""
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    vps.predate_outer_host_mapping()
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    _provision(outer, latchkey_directory, host_id)
    secrets_before = {name: vps.secret(name) for name in vps.secrets_dir_entries()}
    tunnel_key_before = vps.tunnel_key()

    _provision(outer, latchkey_directory, host_id)

    assert {name: vps.secret(name) for name in vps.secrets_dir_entries()} == secrets_before
    assert vps.tunnel_key() == tunnel_key_before
    # The tunnel target and the gateway's address are unchanged, so the tunnel
    # is only made sure to be running.
    assert vps.supervisorctl_calls()[2:] == [f"start {TUNNEL_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]
    assert len(vps.installed_versions) == 2


def test_a_container_that_needs_no_tunnel_gets_none_on_later_passes_either(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    _provision(outer, latchkey_directory, host_id)

    _provision(outer, latchkey_directory, host_id)

    assert vps.tunnel_key() is None
    assert vps.supervisorctl_calls() == [f"restart {GATEWAY_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]


def test_the_package_is_installed_before_the_owner_exec_daemon_is_provisioned(tmp_path: Path) -> None:
    """The package's postinst loads the firewall that keeps both bridge-bound services on the docker bridge.

    The daemon (and the gateway) must never listen unfenced, so the install
    that loads the policy precedes the daemon's provisioning on every pass.
    """
    outer = fake_vps(tmp_path)

    _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    commands = as_vps(outer).recorded_commands()
    install_index = next(index for index, command in enumerate(commands) if "dpkg -i" in command)
    owner_exec_index = next(index for index, command in enumerate(commands) if "owner-exec" in command)
    assert install_index < owner_exec_index


def test_every_machine_command_is_kept_out_of_the_logs(tmp_path: Path) -> None:
    """The documents carry the machine's secrets, and the answers its store, so none is traced."""
    latchkey_directory = _latchkey_directory(tmp_path)
    outer = fake_vps(tmp_path)
    as_vps(outer).hold(
        {"slack": ["a@example.com"]}, key=load_or_create_encryption_key(latchkey_directory).get_secret_value()
    )

    _provision(outer, latchkey_directory, HostId.generate())

    machine_commands = [entry for entry in as_vps(outer).recorded if entry.command.startswith(REMOTE_COMMAND_NAME)]
    # The read (which brought the store back for the desktop key to be tried here) and the apply.
    assert len(machine_commands) == 2
    assert all(entry.is_kept_out_of_logs for entry in machine_commands)


def test_provisioning_refuses_a_machine_with_no_docker_bridge(tmp_path: Path) -> None:
    """Failing closed: a wildcard bind on a VPS would put the gateway on its public interface, password-gated only."""
    outer = fake_vps(tmp_path)
    as_vps(outer).docker_bridge_address = ""

    with pytest.raises(RemoteGatewayError, match="public/wildcard"):
        _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    # Nothing else touched the machine: the address is resolved before anything is installed.
    assert len(as_vps(outer).recorded) == 1
    assert as_vps(outer).written == []


def test_provision_remote_gateway_is_noop_on_local_outer_host(tmp_path: Path) -> None:
    # A local outer (e.g. the local docker daemon's machine) must never be
    # provisioned -- we don't apt/npm-install latchkey on the user's computer.
    outer = fake_vps(tmp_path, is_local=True)

    _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    assert as_vps(outer).recorded == []
    assert as_vps(outer).written == []


def test_provision_remote_gateway_raises_when_the_install_fails(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    as_vps(outer).is_install_failing = True

    with pytest.raises(RemoteGatewayError, match="Unable to locate package nodejs"):
        _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    # Nothing was read or applied on a machine the package did not land on.
    assert len(_latchkey_commands(as_vps(outer))) == 1


def test_provision_remote_gateway_uses_generous_install_timeout(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)

    _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    install = next(entry for entry in as_vps(outer).recorded if "dpkg -i" in entry.command)
    assert install.timeout_seconds == 300.0


def test_provision_remote_gateway_raises_when_container_not_found(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path, container_name="")
    host_id = HostId.generate()

    with pytest.raises(RemoteGatewayError, match=f"no container labeled com.imbue.mngr.host-id={host_id}"):
        _provision(outer, _latchkey_directory(tmp_path), host_id)


def test_provisioning_refuses_a_machine_whose_secrets_directory_is_on_disk(tmp_path: Path) -> None:
    """The key must never land on a disk-backed filesystem, so a machine without a RAM-backed /run is refused."""
    outer = fake_vps(tmp_path)
    as_vps(outer).set_secrets_dir_filesystem_type("ext4")

    with pytest.raises(RemoteGatewayError, match="not RAM-backed"):
        _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    assert as_vps(outer).secrets_dir_entries() == []


def test_a_gateway_that_does_not_come_up_fails_the_pass(tmp_path: Path) -> None:
    """Unlike the tunnel, a gateway that stays down after its secrets were rewritten is a failed provisioning."""
    outer = fake_vps(tmp_path)
    as_vps(outer).fail_supervisorctl_for(GATEWAY_PROGRAM_NAME)

    with pytest.raises(RemoteGatewayError, match="provision the latchkey gateway"):
        _provision(outer, _latchkey_directory(tmp_path), HostId.generate())


# The reverse tunnel, for a container whose agent reaches the gateway at its own loopback.
# CLEANUP: drop this section with the reverse tunnel; see the comment above
# ``CONTAINER_TUNNEL_KEY_FILENAME`` in ``remote/package.py``.


def test_extra_hosts_resolve_the_outer_host_only_by_its_own_mapping() -> None:
    assert _do_extra_hosts_resolve_outer_host((OUTER_HOST_EXTRA_HOST,))
    assert _do_extra_hosts_resolve_outer_host(("registry.internal:10.0.0.7", OUTER_HOST_EXTRA_HOST))
    # An older container has no extra hosts at all; some unrelated mapping does not count.
    assert not _do_extra_hosts_resolve_outer_host(())
    assert not _do_extra_hosts_resolve_outer_host(("registry.internal:10.0.0.7",))


def test_the_tunnel_decision_needs_a_read_that_looked_at_the_container(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    host_id = HostId.generate()
    state = read_remote_state(outer, RemoteStateRequest(), failure_description="read")

    with pytest.raises(RemoteGatewayError, match="was not asked how its container was created"):
        _does_container_need_reverse_tunnel(outer, host_id, state)


def test_provision_remote_gateway_keeps_the_reverse_tunnel_for_a_container_without_the_mapping(
    tmp_path: Path,
) -> None:
    """A container created before the outer-host mapping still reaches the gateway at its own loopback.

    Its ``LATCHKEY_GATEWAY`` names ``127.0.0.1`` and neither that nor the
    missing ``--add-host`` can change for the life of the container, so the
    reverse tunnel is wired for it -- pointed at the bridge address the
    gateway binds.
    """
    outer = fake_vps(tmp_path, container_name="mngr-ws-old")
    vps = as_vps(outer)
    vps.predate_outer_host_mapping()
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    # The gateway itself is wired exactly as for a new container; then the
    # keypair is minted and authorized in this container, the target written
    # for the tunnel program, and both programs (re)started.
    assert vps.gateway_conf() == f"LK_GATEWAY_LISTEN_HOST='{DEFAULT_DOCKER_BRIDGE_ADDRESS}'\n"
    public_key = (vps.latchkey_dir / f"{CONTAINER_TUNNEL_KEY_FILENAME}.pub").read_text().strip()
    assert public_key in vps.container_authorized_keys("mngr-ws-old", "root")
    assert vps.tunnel_conf() == "LK_CONTAINER_SSH_USER='root'\nLK_CONTAINER_SSH_PORT='2222'\n"
    assert vps.supervisorctl_calls() == [f"restart {TUNNEL_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]


def test_provision_remote_gateway_keeps_a_wired_reverse_tunnel_for_a_container_with_the_mapping(
    tmp_path: Path,
) -> None:
    """A tunnel an older client wired is kept and re-pointed at the address the gateway binds.

    The container carries the mapping, but its agent's ``LATCHKEY_GATEWAY`` was
    decided by the client that created its host, which may name the loopback;
    the keypair that client minted for the tunnel is what says so, and the
    tunnel is never dropped.
    """
    outer = fake_vps(tmp_path, container_name="mngr-ws-kept")
    vps = as_vps(outer)
    vps.mint_tunnel_key()
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    # The old keypair is what the container authorizes; nothing was re-minted.
    assert vps.tunnel_key() == "FAKE PRIVATE KEY earlier\n"
    assert "ssh-ed25519 FAKEearlier fake@vps" in vps.container_authorized_keys("mngr-ws-kept", "root")
    assert vps.tunnel_conf() == "LK_CONTAINER_SSH_USER='root'\nLK_CONTAINER_SSH_PORT='2222'\n"
    assert vps.supervisorctl_calls() == [f"restart {TUNNEL_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]


def test_a_changed_bridge_address_bounces_the_tunnel_with_the_gateway(tmp_path: Path) -> None:
    """The tunnel forwards to the address the gateway binds, so a change to it has to reach the running tunnel."""
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    vps.predate_outer_host_mapping()
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    _provision(outer, latchkey_directory, host_id)
    vps.docker_bridge_address = "172.18.0.1"

    _provision(outer, latchkey_directory, host_id)

    assert vps.gateway_conf() == "LK_GATEWAY_LISTEN_HOST='172.18.0.1'\n"
    assert vps.supervisorctl_calls()[2:] == [f"restart {TUNNEL_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]


def test_a_rewired_container_restarts_the_tunnel(tmp_path: Path) -> None:
    """The tunnel program is static; a changed target has to bounce it to take effect."""
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    vps.predate_outer_host_mapping()
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    _provision(outer, latchkey_directory, host_id)

    provision_remote_gateway(
        outer,
        host_id=host_id,
        container_ssh_user="agent",
        container_ssh_port=2223,
        latchkey=_desktop_latchkey(latchkey_directory),
        desktop_secrets=_DESKTOP_SECRETS,
        package_layout=vps.layout,
    )

    assert vps.tunnel_conf() == "LK_CONTAINER_SSH_USER='agent'\nLK_CONTAINER_SSH_PORT='2223'\n"
    assert vps.supervisorctl_calls()[2] == f"restart {TUNNEL_PROGRAM_NAME}"
    assert vps.container_authorized_keys("mngr-ws", "agent") != ""


def test_a_tunnel_that_does_not_come_up_at_once_is_left_to_supervisord(tmp_path: Path) -> None:
    """The tunnel's ssh may exit before it counts as started; supervisord retries it, so the pass still lands."""
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    vps.predate_outer_host_mapping()
    vps.fail_supervisorctl_for(TUNNEL_PROGRAM_NAME)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    assert vps.tunnel_conf() == "LK_CONTAINER_SSH_USER='root'\nLK_CONTAINER_SSH_PORT='2222'\n"
    assert vps.supervisorctl_calls() == [f"restart {TUNNEL_PROGRAM_NAME}", f"restart {GATEWAY_PROGRAM_NAME}"]
    assert vps.secret(GATEWAY_ENCRYPTION_KEY_FILENAME) is not None


# The machine's own encryption key.


def test_a_fresh_machine_gets_a_key_of_its_own(tmp_path: Path) -> None:
    """A machine's credentials are readable by that machine and the desktops managing it, not by every VPS."""
    outer = fake_vps(tmp_path)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    key = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert key is not None
    desktop_key = load_or_create_encryption_key(latchkey_directory).get_secret_value()
    assert key.get_secret_value() != desktop_key
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == key.get_secret_value()
    assert not as_vps(outer).has_received(desktop_key)


def test_the_key_the_machine_is_running_under_is_adopted_never_replaced(tmp_path: Path) -> None:
    """Another install may have provisioned the machine; its running key is adopted.

    Minting (or guessing) a key here instead would re-key the machine out from
    under the install that provisioned it, breaking the store both installs
    are supposed to share. The record here is only a mirror of the machine's
    key, so a stale one is corrected too.
    """
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under_key("other-installs-key-7841\n")
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    store_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id, SecretStr("stale-record-1189"))

    _provision(outer, latchkey_directory, host_id)

    recorded = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert recorded is not None and recorded.get_secret_value() == "other-installs-key-7841"
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == "other-installs-key-7841"


def test_a_rebooted_machine_is_handed_back_the_key_its_store_is_written_under(tmp_path: Path) -> None:
    """A reboot wipes the RAM-backed copy; the record here is what puts it back."""
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]}, machine_permissions=_GRANTED_HERE)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    store_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id, SecretStr("machine-key-5518"))

    _provision(outer, latchkey_directory, host_id)

    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == "machine-key-5518"
    assert as_vps(outer).machine_accounts() == {"slack": ["a@example.com"]}


def test_a_rebooted_legacy_machine_keeps_the_desktop_key_that_opens_its_store(tmp_path: Path) -> None:
    """A store the desktop key actually opens is our own legacy machine's, so the key is kept and the store stays readable."""
    latchkey_directory = _latchkey_directory(tmp_path)
    desktop_key = load_or_create_encryption_key(latchkey_directory)
    outer = fake_vps(tmp_path)
    as_vps(outer).hold({"slack": ["a@example.com"]}, key=desktop_key.get_secret_value())
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) == desktop_key
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == desktop_key.get_secret_value()
    # The store opened, so nothing was abandoned; the key was tried here, not on the machine.
    assert as_vps(outer).machine_accounts() == {"slack": ["a@example.com"]}
    assert len([command for command in _latchkey_commands(as_vps(outer)) if "read-state" in command]) == 1
    assert as_vps(outer).latchkey_calls() == []


def test_a_store_nobody_present_can_read_is_abandoned_for_a_fresh_key(tmp_path: Path) -> None:
    """Provisioned by a computer that is gone, rebooted since: signing in again is possible, waiting is not."""
    latchkey_directory = _latchkey_directory(tmp_path)
    outer = fake_vps(tmp_path)
    as_vps(outer).hold({"slack": ["lost@example.com"]}, key="a-key-only-the-lost-computer-held")
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    key = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert key is not None
    # A fresh key of the machine's own -- neither the unreadable store's nor the desktop's.
    assert key.get_secret_value() != "a-key-only-the-lost-computer-held"
    desktop_key = load_or_create_encryption_key(latchkey_directory).get_secret_value()
    assert key.get_secret_value() != desktop_key
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == key.get_secret_value()
    # The unreadable store is gone rather than left to break the freshly-keyed gateway.
    assert CREDENTIALS_STORE_FILENAME not in as_vps(outer).latchkey_dir_entries()
    # The desktop's key was tried here and turned out not to be the machine's, so it never went there.
    assert not as_vps(outer).has_received(desktop_key)


def test_a_store_the_desktop_key_cannot_be_tried_against_is_kept(tmp_path: Path) -> None:
    """Only a key the store refuses reads as "nobody present can read it"; any other failure fails the pass.

    A store is abandoned when no key anyone holds opens it, so a store this
    computer merely failed to try (one it cannot parse, say) must never be
    taken for one.
    """
    latchkey_directory = _latchkey_directory(tmp_path)
    outer = fake_vps(tmp_path)
    vps = as_vps(outer)
    vps.hold({"slack": ["a@example.com"]}, key="a-key-only-the-lost-computer-held")
    (vps.latchkey_dir / CREDENTIALS_STORE_FILENAME).write_bytes(b"not a store this computer can parse")
    host_id = HostId.generate()

    with pytest.raises(RemoteGatewayError, match="Failed to try the desktop's key"):
        _provision(outer, latchkey_directory, host_id)

    assert CREDENTIALS_STORE_FILENAME in vps.latchkey_dir_entries()
    assert not any("apply-state" in command for command in _latchkey_commands(vps))
    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) is None


def test_the_store_travels_only_when_no_key_is_recorded_for_the_machine(tmp_path: Path) -> None:
    """With the machine's key recorded here, nothing is decided from the store, so the read leaves it on the machine."""
    outer = fake_vps(tmp_path, {"slack": ["a@example.com"]})
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    store_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id, SecretStr("machine-key-5518"))

    _provision(outer, latchkey_directory, host_id)

    read = next(command for command in _latchkey_commands(as_vps(outer)) if "read-state" in command)
    assert "include_credential_store" not in read


def test_a_decided_key_is_recorded_only_once_the_machine_has_taken_it(tmp_path: Path) -> None:
    """A pass that fails before its apply leaves no record, so the next pass decides afresh.

    The apply that hands a decided key over is also what removes a store
    nobody can read; a record written ahead of a failed apply would have the
    next pass hand the machine that key while its unreadable store stays.
    """
    latchkey_directory = _latchkey_directory(tmp_path)
    outer = fake_vps(tmp_path)
    as_vps(outer).hold({"slack": ["lost@example.com"]}, key="a-key-only-the-lost-computer-held")
    as_vps(outer).hold_config("not json")
    host_id = HostId.generate()

    with pytest.raises(RemoteGatewayError, match="Failed to update the latchkey config"):
        _provision(outer, latchkey_directory, host_id)

    assert stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id) is None
    assert CREDENTIALS_STORE_FILENAME in as_vps(outer).latchkey_dir_entries()

    as_vps(outer).hold_config("{}")
    _provision(outer, latchkey_directory, host_id)

    key = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert key is not None
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == key.get_secret_value()
    assert CREDENTIALS_STORE_FILENAME not in as_vps(outer).latchkey_dir_entries()


def test_a_storeless_machine_an_older_build_provisioned_gets_a_key_of_its_own(tmp_path: Path) -> None:
    """With no store to try the desktop's key against, nothing says the machine is ours, so it never gets that key."""
    latchkey_directory = _latchkey_directory(tmp_path)
    outer = fake_vps(tmp_path, machine_permissions=_GRANTED_HERE)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    key = stored_machine_encryption_key(plugin_data_dir(latchkey_directory), host_id)
    assert key is not None
    desktop_key = load_or_create_encryption_key(latchkey_directory).get_secret_value()
    assert key.get_secret_value() != desktop_key
    assert as_vps(outer).secret(GATEWAY_ENCRYPTION_KEY_FILENAME) == key.get_secret_value()
    assert not as_vps(outer).has_received(desktop_key)


# The machine's own gateway listen password.


def test_a_machine_nobody_has_provisioned_yet_takes_this_computers_password(tmp_path: Path) -> None:
    """A brand-new machine takes this computer's password: it is what its workspaces present."""
    outer = fake_vps(tmp_path)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    assert as_vps(outer).secret(GATEWAY_LISTEN_PASSWORD_FILENAME) == "desktop-password"
    # Recorded durably, so a reboot that wipes the machine's copy does not lose it.
    assert stored_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id) == "desktop-password"


def test_provisioning_keeps_the_machines_password_while_replacing_the_desktops(tmp_path: Path) -> None:
    """Moving to another computer must not re-key the gateway its workspaces authenticate to.

    Another of the user's computers created this machine's workspaces, and
    their env file is fixed: writing this computer's own password here would
    answer every request they make with a 401. The secrets for the hop back to
    the user's computer become this computer's.
    """
    outer = fake_vps(tmp_path)
    as_vps(outer).run_under(GATEWAY_LISTEN_PASSWORD_FILENAME, "other-computers-password\n")
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()

    _provision(outer, latchkey_directory, host_id)

    assert as_vps(outer).secret(GATEWAY_LISTEN_PASSWORD_FILENAME) == "other-computers-password"
    assert as_vps(outer).secret(DESKTOP_GATEWAY_PASSWORD_FILENAME) == "desktop-password"
    assert as_vps(outer).secret(DESKTOP_PERMISSIONS_OVERRIDE_FILENAME) == "desktop-override-jwt"
    assert stored_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id) == "other-computers-password"


def test_a_rebooted_machine_is_handed_back_its_recorded_password(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    store_machine_gateway_password(plugin_data_dir(latchkey_directory), host_id, "the-password-it-was-created-with")

    _provision(outer, latchkey_directory, host_id)

    assert as_vps(outer).secret(GATEWAY_LISTEN_PASSWORD_FILENAME) == "the-password-it-was-created-with"


# The gateway's config.


def test_provisioning_preserves_the_machines_existing_config(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    as_vps(outer).hold_config('{"settings": {"theme": "dark"}, "registeredServices": {"acme": {"baseApiUrl": "x"}}}')

    _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    config = json.loads(as_vps(outer).machine_config() or "{}")
    assert config["settings"]["theme"] == "dark"
    assert config["settings"]["hideBuiltinServices"] == ["notion"]
    assert config["registeredServices"]["acme"] == {"baseApiUrl": "x"}
    assert set(additional_service_registration_entries()) <= set(config["registeredServices"])


def test_provisioning_registers_the_custom_services_this_computer_has(tmp_path: Path) -> None:
    """A user-created service registered here reaches the machine's config, which holds none of its own.

    Only this package's half of the config travels: the browser and keyring
    settings in this computer's file belong to this computer.
    """
    latchkey_directory = _latchkey_directory(tmp_path)
    (latchkey_directory / CONFIG_FILENAME).write_text(
        json.dumps(
            {
                "browser": {"executablePath": "/Applications/Chromium.app", "source": "system"},
                "registeredServices": {
                    "custom_example_com": build_custom_service_registration("example.com", "https")
                },
            }
        )
    )
    outer = fake_vps(tmp_path)

    _provision(outer, latchkey_directory, HostId.generate())

    config = json.loads(as_vps(outer).machine_config() or "{}")
    assert config["registeredServices"]["custom_example_com"] == {"baseApiUrl": "https://example.com/"}
    assert set(additional_service_registration_entries()) <= set(config["registeredServices"])
    assert "browser" not in config


def test_provisioning_raises_on_an_invalid_remote_config(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path)
    as_vps(outer).hold_config("not json")

    with pytest.raises(RemoteGatewayError, match="Failed to update the latchkey config"):
        _provision(outer, _latchkey_directory(tmp_path), HostId.generate())

    # Nothing was applied to a machine whose config could not be merged.
    assert as_vps(outer).secrets_dir_entries() == []


# Permission reconciliation.


def test_provisioning_seeds_a_machine_with_no_policy_from_the_local_file(tmp_path: Path) -> None:
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    outer = fake_vps(tmp_path)

    _provision(outer, latchkey_directory, host_id)

    assert as_vps(outer).machine_permissions() == _GRANTED_HERE


def test_provisioning_adopts_the_policy_the_machine_holds(tmp_path: Path) -> None:
    """The machine's file is the rendezvous: it is how a second computer sees what the first granted.

    A local edit is deliberately *not* pushed by this pass -- it is pushed when
    it is made -- so a machine that already has a policy is only ever adopted
    from here, never written over by a stale local copy.
    """
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    outer = fake_vps(tmp_path, machine_permissions=_GRANTED_ELSEWHERE)

    _provision(outer, latchkey_directory, host_id)

    assert local_path.read_text() == _GRANTED_ELSEWHERE
    assert as_vps(outer).machine_permissions() == _GRANTED_ELSEWHERE


def test_provisioning_leaves_an_unchanged_local_copy_untouched(tmp_path: Path) -> None:
    """Adopting an identical policy must not rewrite the local file (nothing to say, nothing to churn)."""
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    modified_at_before = local_path.stat().st_mtime_ns
    outer = fake_vps(tmp_path, machine_permissions=_GRANTED_HERE)

    _provision(outer, latchkey_directory, host_id)

    assert local_path.stat().st_mtime_ns == modified_at_before


def test_provisioning_refuses_a_policy_it_cannot_read_rather_than_storing_it(tmp_path: Path) -> None:
    """A policy this build cannot parse must not replace one it can enforce."""
    latchkey_directory = _latchkey_directory(tmp_path)
    host_id = HostId.generate()
    local_path = _write_local_permissions(latchkey_directory, host_id, _GRANTED_HERE)
    outer = fake_vps(tmp_path, machine_permissions='{"rules": "not-a-list"}')

    with pytest.raises(RemoteGatewayError, match="cannot read"):
        _provision(outer, latchkey_directory, host_id)

    assert local_path.read_text() == _GRANTED_HERE


# What a read answers about the container.


def test_a_read_asked_about_the_container_answers_how_it_was_created(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path, container_name="mngr-ws-2")
    host_id = HostId.generate()

    state = read_remote_state(outer, RemoteStateRequest(container_host_id=host_id), failure_description="read")

    assert state.container_extra_hosts == (OUTER_HOST_EXTRA_HOST,)
    assert as_vps(outer).docker_calls() == [
        f"ps -a --filter label=com.imbue.mngr.host-id={host_id} --format {{{{.Names}}}}",
        "inspect -f {{json .HostConfig.ExtraHosts}} mngr-ws-2",
    ]

    # A container created with no mapping at all (docker prints ``null``), and one an earlier pass tunneled into.
    as_vps(outer).predate_outer_host_mapping()
    as_vps(outer).mint_tunnel_key()
    state_of_older = read_remote_state(
        outer, RemoteStateRequest(container_host_id=host_id), failure_description="read"
    )
    assert state_of_older.container_extra_hosts == ()
    assert state_of_older.has_container_tunnel_key is True


def test_a_read_asked_about_a_container_the_machine_lacks_fails(tmp_path: Path) -> None:
    outer = fake_vps(tmp_path, container_name="")
    host_id = HostId.generate()

    with pytest.raises(RemoteGatewayError, match=f"no container labeled com.imbue.mngr.host-id={host_id}"):
        read_remote_state(outer, RemoteStateRequest(container_host_id=host_id), failure_description="read")
