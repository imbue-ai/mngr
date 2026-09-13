import subprocess
from pathlib import Path

import pytest
from pydantic import Field

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.errors import SliceCommandError
from imbue.mngr_imbue_cloud.errors import SliceReserveOutputError
from imbue.mngr_imbue_cloud.slices.qemu_slice_client import QemuSliceVpsClient
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus

# Note: the full provision_slice_vm flow drives the box over SSH and is
# exercised by the live slice smoke flow, not here. These unit tests pin the
# parts that need no box: the interface contract, the SSH command construction,
# provision's failure-cleanup contract, and the status/list parsing (via a
# scripted command recorder).


def _client() -> QemuSliceVpsClient:
    return QemuSliceVpsClient(
        box_address="box.example",
        box_ssh_user="slicehost",
        private_key_path="/tmp/id",
        box_host_public_key="ssh-ed25519 AAAAtestboxhostkey",
    )


def test_get_instance_ip_is_the_box_address() -> None:
    # The slice's sshd is DNAT-forwarded on the box's interface, so external
    # consumers (and the laptop-side bake) reach it at the box's address.
    client = _client()
    assert client.get_instance_ip(VpsInstanceId("mngr-slice-x")) == "box.example"


def test_box_ssh_command_targets_the_slice_user_with_the_pool_key() -> None:
    client = _client()
    command = client._box_ssh_command("systemctl is-active mngr-slice@1", Path("/tmp/known_hosts"))
    assert command[0] == "ssh"
    assert "-i" in command and "/tmp/id" in command
    assert "slicehost@box.example" in command
    # The box host key is pinned strictly (no trust-on-first-use).
    assert "StrictHostKeyChecking=yes" in command
    assert any(arg.startswith("UserKnownHostsFile=") for arg in command)
    assert "StrictHostKeyChecking=accept-new" not in command
    # The remote command is the last arg, prefixed with an explicit PATH so a
    # non-login shell still finds the box tooling in /usr/local/bin.
    assert command[-1].endswith("systemctl is-active mngr-slice@1")
    assert "/usr/local/bin" in command[-1]


def test_box_ssh_command_quotes_a_known_hosts_path_containing_a_space(tmp_path: Path) -> None:
    """ssh splits UserKnownHostsFile on whitespace, so the pinned path needs its own quotes.

    The known_hosts file is written beside the pool key, so a key directory whose
    name contains a space produces a spaced path here without anyone choosing one.
    """
    key_dir = tmp_path / "pool keys"
    key_dir.mkdir()
    client = QemuSliceVpsClient(
        box_address="box.example",
        box_ssh_user="slicehost",
        private_key_path=str(key_dir / "id"),
        box_host_public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 20,
    )
    known_hosts_path = client._box_known_hosts_file()
    assert known_hosts_path.parent == key_dir
    command = client._box_ssh_command("systemctl is-active mngr-slice@1", known_hosts_path)
    option = next(arg for arg in command if arg.startswith("UserKnownHostsFile="))
    value = option.removeprefix("UserKnownHostsFile=")
    assert value.startswith('"') and value.endswith('"'), option
    assert " " in value, option


def test_box_ssh_command_requires_a_private_key() -> None:
    client = QemuSliceVpsClient(box_address="box.example", box_ssh_user="slicehost", private_key_path=None)
    with pytest.raises(SliceCommandError):
        client._box_ssh_command("systemctl is-active mngr-slice@1", Path("/tmp/known_hosts"))


def test_box_ssh_remote_string_stays_valid_bash_for_compound_commands() -> None:
    # The disk listing is a `for` loop; a bare assignment prefix cannot precede
    # a compound statement, so the PATH must ride a standalone `export`.
    client = _client()
    compound = 'for d in /srv/x/*/datadisk.qcow2; do [ -e "$d" ] || continue; basename "$(dirname "$d")"; done'
    remote_string = client._box_ssh_command(compound, Path("/tmp/known_hosts"))[-1]
    parse_result = subprocess.run(["bash", "-n", "-c", remote_string], capture_output=True, text=True)
    assert parse_result.returncode == 0, parse_result.stderr


class _RecordingClient(QemuSliceVpsClient):
    """QemuSliceVpsClient whose box SSH is replaced by a scripted command recorder.

    Lets teardown/status/list logic be unit-tested without a real box: each
    remote command returns the (returncode, stdout, stderr) the test scripts by
    substring.
    """

    scripted_responses: dict[str, tuple[int | None, str, str]] = Field(default_factory=dict)
    recorded_commands: list[str] = Field(default_factory=list)

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        self.recorded_commands.append(remote_command)
        for substring, response in self.scripted_responses.items():
            if substring in remote_command:
                return response
        return 0, "", ""


def _recording_client(scripted_responses: dict[str, tuple[int | None, str, str]] | None = None) -> _RecordingClient:
    return _RecordingClient(
        box_address="box.example",
        box_ssh_user="slicehost",
        private_key_path="/tmp/id",
        box_host_public_key="ssh-ed25519 AAAAtestboxhostkey",
        scripted_responses=scripted_responses or {},
    )


def test_provision_destroys_the_reserved_slice_when_the_marker_is_garbled() -> None:
    # A rc-0 reserve whose stdout carries no parseable RESERVED marker has still
    # claimed the slot on the box; the failure must run the best-effort destroy
    # (and never reach the start command) instead of leaking the reservation.
    client = _recording_client({"base64 -d | bash": (0, "no marker here", "")})
    with pytest.raises(SliceReserveOutputError):
        client.provision_slice_vm(
            host_id=HostId.generate(),
            env_name="dev",
            vcpus=1,
            memory_mib=8192 - 512,
            disk_gib=10,
            host_dir="/root/.mngr",
            root_authorized_public_key="ssh-ed25519 AAAAbake",
            host_private_key_pem="pem",
            host_public_key_openssh="ssh-ed25519 AAAAhost",
            boot_disk_gib=8,
            slot_count=2,
            port_range_start=22000,
            port_range_end=22010,
            units=8,
            box_total_units=120,
            box_disk_budget_gib=400,
        )
    # Exactly the failed reserve and the follow-up destroy, both shipped scripts.
    assert len(client.recorded_commands) == 2
    assert not any("systemctl start" in command for command in client.recorded_commands)


def test_provision_refuses_a_carve_with_neither_a_static_root_key_nor_a_ca() -> None:
    # A VM that authorizes no static root key and trusts no CA would boot
    # unreachable; the carve must refuse before touching the box (like the lima
    # client does), not fail minutes later at the sshd wait.
    client = _recording_client()
    with pytest.raises(BareMetalProvisioningError, match="root access path"):
        client.provision_slice_vm(
            host_id=HostId.generate(),
            env_name="dev",
            vcpus=1,
            memory_mib=8192 - 512,
            disk_gib=10,
            host_dir="/root/.mngr",
            root_authorized_public_key=None,
            host_private_key_pem="pem",
            host_public_key_openssh="ssh-ed25519 AAAAhost",
            boot_disk_gib=8,
            slot_count=2,
            port_range_start=22000,
            port_range_end=22010,
            units=8,
            box_total_units=120,
            box_disk_budget_gib=400,
        )
    assert client.recorded_commands == []


def test_provision_requires_the_machine_sizing_knobs() -> None:
    # A gen-2 carve without the sizing knobs (or with a memory_mib that
    # disagrees with the units) must refuse before touching the box.
    client = _recording_client()
    with pytest.raises(BareMetalProvisioningError):
        client.provision_slice_vm(
            host_id=HostId.generate(),
            env_name="dev",
            vcpus=1,
            memory_mib=8192 - 512,
            disk_gib=10,
            host_dir="/root/.mngr",
            root_authorized_public_key="ssh-ed25519 AAAAbake",
            host_private_key_pem="pem",
            host_public_key_openssh="ssh-ed25519 AAAAhost",
            boot_disk_gib=8,
            slot_count=2,
            port_range_start=22000,
            port_range_end=22010,
        )
    with pytest.raises(BareMetalProvisioningError):
        client.provision_slice_vm(
            host_id=HostId.generate(),
            env_name="dev",
            vcpus=1,
            memory_mib=1024,
            disk_gib=10,
            host_dir="/root/.mngr",
            root_authorized_public_key="ssh-ed25519 AAAAbake",
            host_private_key_pem="pem",
            host_public_key_openssh="ssh-ed25519 AAAAhost",
            boot_disk_gib=8,
            slot_count=2,
            port_range_start=22000,
            port_range_end=22010,
            units=8,
            box_total_units=120,
            box_disk_budget_gib=400,
        )
    assert client.recorded_commands == []


def test_destroy_instance_ships_the_teardown_script() -> None:
    client = _recording_client()
    client.destroy_instance(VpsInstanceId("mngr-slice-dev-x-abc"))
    # The teardown ships base64-encoded (quoting-proof) and is tolerant by
    # construction, so one recorded command suffices.
    assert len(client.recorded_commands) == 1
    assert "base64 -d | bash" in client.recorded_commands[0]


def test_destroy_instance_raises_on_failure() -> None:
    client = _recording_client({"base64 -d | bash": (1, "", "boom")})
    with pytest.raises(SliceCommandError):
        client.destroy_instance(VpsInstanceId("mngr-slice-dev-x-abc"))


def test_list_instance_names_parses_one_name_per_line() -> None:
    client = _recording_client({"ls -1": (0, "mngr-slice-a\nmngr-slice-b\n\n", "")})
    assert client.list_instance_names() == {"mngr-slice-a", "mngr-slice-b"}


def test_list_instance_observations_reports_malformed_box_output_as_a_slice_command_error() -> None:
    # The shared parser raises the subpackage's own error type; callers of the
    # client only handle SliceCommandError, so the client must translate.
    well_formed = _recording_client({"MNGR_SLICE_NOW": (0, "MNGR_SLICE_NOW 1000\nmngr-slice-a active 400\n", "")})
    (observation,) = well_formed.list_instance_observations()
    assert (observation.instance_name, observation.is_active, observation.age_seconds) == ("mngr-slice-a", True, 600.0)
    malformed = _recording_client({"MNGR_SLICE_NOW": (0, "MNGR_SLICE_NOW 1000\nmngr-slice-a active\n", "")})
    with pytest.raises(SliceCommandError):
        malformed.list_instance_observations()


def test_list_disk_names_derives_the_data_suffix() -> None:
    client = _recording_client({"datadisk.qcow2": (0, "mngr-slice-a\n", "")})
    assert client.list_disk_names() == {"mngr-slice-a-data"}


def test_list_disk_names_on_a_box_with_no_slices_is_empty() -> None:
    # The rendered loop must survive a glob that matches nothing (a freshly
    # prepped or fully drained box): empty listing, not a failed command.
    client = _recording_client({"datadisk.qcow2": (0, "", "")})
    assert client.list_disk_names() == set()
    assert "|| continue" in client.recorded_commands[0]


def test_get_instance_status_maps_systemd_states() -> None:
    active_client = _recording_client({"is-active": (0, "active\n", "")})
    assert active_client.get_instance_status(VpsInstanceId("mngr-slice-a")) == VpsInstanceStatus.ACTIVE
    stopped_client = _recording_client({"is-active": (0, "inactive\n", "")})
    assert stopped_client.get_instance_status(VpsInstanceId("mngr-slice-a")) == VpsInstanceStatus.HALTED
    absent_client = _recording_client({"is-active": (0, "absent\n", "")})
    assert absent_client.get_instance_status(VpsInstanceId("mngr-slice-a")) == VpsInstanceStatus.UNKNOWN


def test_read_management_trust_counts_static_keys_and_reads_the_trusted_ca() -> None:
    client = _recording_client(
        {
            "MNGR_MANAGEMENT_TRUST_SPLIT": (
                0,
                "ssh-ed25519 AAAA one\n# comment\n\nssh-ed25519 BBBB two\nMNGR_MANAGEMENT_TRUST_SPLIT\n"
                "ssh-ed25519 CCCC tier-ca\n",
                "",
            )
        }
    )
    trust = client.read_management_trust()
    assert trust.authorized_key_count == 2
    assert trust.trusted_ca_public_key == "ssh-ed25519 CCCC tier-ca"


def test_read_management_trust_reports_no_ca_and_no_keys_for_a_bare_box() -> None:
    client = _recording_client({"MNGR_MANAGEMENT_TRUST_SPLIT": (0, "MNGR_MANAGEMENT_TRUST_SPLIT\n", "")})
    trust = client.read_management_trust()
    assert (trust.authorized_key_count, trust.trusted_ca_public_key) == (0, None)


def test_read_box_health_texts_splits_on_the_marker() -> None:
    client = _recording_client({"/proc/mdstat": (0, "md0 : active raid1\nMNGR_BOX_HEALTH_SPLIT\nFilename Type\n", "")})
    mdstat_text, swaps_text = client.read_box_health_texts()
    assert "md0" in mdstat_text
    assert "Filename" in swaps_text


def test_run_on_box_raises_its_own_error_without_a_pinned_host_key() -> None:
    # A box mid-reinstall has no recorded host key yet; the audit reports such a
    # box as unaudited by catching SliceCommandError, so it must not arrive
    # wrapped in the concurrency group's exception group.
    client = QemuSliceVpsClient(box_address="box.example", box_ssh_user="slicehost", private_key_path="/tmp/id")
    with pytest.raises(SliceCommandError, match="no pinned host key"):
        client.run_on_box("true", timeout=5.0, label="test")
