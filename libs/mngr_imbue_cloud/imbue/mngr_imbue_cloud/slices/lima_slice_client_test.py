import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_vps.primitives import VpsInstanceId

# Note: provision_slice_vm / destroy_instance / get_instance_status drive limactl
# over SSH against a real box and are exercised by the live slice smoke test, not
# here. These unit tests pin the parts that need no box: the interface contract,
# the SSH command construction, and the listening-port parsing.


def _client() -> LimaSliceVpsClient:
    return LimaSliceVpsClient(
        box_address="box.example",
        box_ssh_user="limahost",
        private_key_path="/tmp/id",
        box_host_public_key="ssh-ed25519 AAAAtestboxhostkey",
    )


def test_cloud_only_operations_raise_not_implemented() -> None:
    client = _client()
    with pytest.raises(NotImplementedError):
        client.create_instance("label", "region", "plan", "", (), {})
    with pytest.raises(NotImplementedError):
        client.upload_ssh_key("name", "ssh-ed25519 AAAA")


def test_get_instance_ip_is_the_box_address() -> None:
    # The slice's sshd is forwarded on the box's interface, so external consumers
    # (and the laptop-side bake) reach it at the box's address, not loopback.
    client = _client()
    assert client.get_instance_ip(VpsInstanceId("mngr-slice-x")) == "box.example"


def test_box_ssh_command_targets_the_lima_user_with_the_pool_key() -> None:
    client = _client()
    command = client._box_ssh_command("limactl list --json")
    assert command[0] == "ssh"
    assert "-i" in command and "/tmp/id" in command
    assert "limahost@box.example" in command
    # The box host key is pinned strictly (no trust-on-first-use).
    assert "StrictHostKeyChecking=yes" in command
    assert any(arg.startswith("UserKnownHostsFile=") for arg in command)
    assert "StrictHostKeyChecking=accept-new" not in command
    # The remote command is the last arg, prefixed with an explicit PATH so a
    # non-login shell still finds limactl (extracted to /usr/local/bin by prep).
    assert command[-1].endswith("limactl list --json")
    assert "/usr/local/bin" in command[-1]


def test_box_ssh_command_requires_a_private_key() -> None:
    client = LimaSliceVpsClient(box_address="box.example", box_ssh_user="limahost", private_key_path=None)
    with pytest.raises(LimaCommandError):
        client._box_ssh_command("limactl list --json")


class _RecordingClient(LimaSliceVpsClient):
    """LimaSliceVpsClient whose box SSH is replaced by a scripted command recorder.

    Lets the teardown logic be unit-tested without a real box: each remote command
    returns the (returncode, stdout, stderr) the test scripts by substring.
    """

    scripted_responses: dict[str, tuple[int | None, str, str]] = {}
    recorded_commands: list[str] = []

    def _run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        self.recorded_commands.append(remote_command)
        for substring, response in self.scripted_responses.items():
            if substring in remote_command:
                return response
        return 0, "", ""


def _recording_client(scripted_responses: dict[str, tuple[int | None, str, str]]) -> _RecordingClient:
    return _RecordingClient(
        box_address="box.example",
        box_ssh_user="limahost",
        private_key_path="/tmp/id",
        scripted_responses=scripted_responses,
        recorded_commands=[],
    )


def test_destroy_instance_deletes_disk_when_instance_already_absent() -> None:
    # A carve can fail after the disk was created but before the VM was registered.
    # `limactl delete` then fails ("not found"), but the disk MUST still be deleted
    # so the box slot's data area is not leaked.
    client = _recording_client({"limactl delete": (1, "", "instance not found")})
    client.destroy_instance(VpsInstanceId("mngr-slice-abc"))
    recorded = client.recorded_commands
    assert any("limactl delete --force" in cmd for cmd in recorded)
    assert any("limactl disk delete --force" in cmd and "mngr-slice-abc-data" in cmd for cmd in recorded)


def test_destroy_instance_raises_on_genuine_delete_failure() -> None:
    client = _recording_client({"limactl delete": (1, "", "permission denied")})
    with pytest.raises(LimaCommandError):
        client.destroy_instance(VpsInstanceId("mngr-slice-abc"))


def test_list_disk_names_parses_jsonl_names() -> None:
    disk_json = '{"name": "mngr-slice-aaa-data"}\n{"name": "mngr-slice-bbb-data"}\n'
    client = _recording_client({"limactl disk list --json": (0, disk_json, "")})
    assert client.list_disk_names() == {"mngr-slice-aaa-data", "mngr-slice-bbb-data"}


def test_destroy_disk_unlocks_then_force_deletes() -> None:
    # A leaked orphan disk is typically locked, so destroy_disk must unlock first.
    client = _recording_client({})
    client.destroy_disk("mngr-slice-abc-data")
    recorded = client.recorded_commands
    unlock_idx = next(
        i for i, cmd in enumerate(recorded) if "limactl disk unlock" in cmd and "mngr-slice-abc-data" in cmd
    )
    delete_idx = next(
        i for i, cmd in enumerate(recorded) if "limactl disk delete --force" in cmd and "mngr-slice-abc-data" in cmd
    )
    assert unlock_idx < delete_idx


def test_destroy_disk_tolerates_already_absent_disk() -> None:
    client = _recording_client({"limactl disk delete": (1, "", "disk does not exist")})
    # An already-absent disk must not raise.
    client.destroy_disk("mngr-slice-abc-data")


def test_destroy_disk_raises_on_genuine_delete_failure() -> None:
    client = _recording_client({"limactl disk delete": (1, "", "permission denied")})
    with pytest.raises(LimaCommandError):
        client.destroy_disk("mngr-slice-abc-data")


def test_provision_slice_vm_reserves_under_lock_then_starts_and_returns_box_chosen_ports() -> None:
    host_id = HostId.generate()
    # The reserve runs as one base64'd bash command (it holds the box lock); it prints
    # the ports it chose. The long boot is a separate, unlocked `limactl start`.
    client = _recording_client(
        {
            "base64 -d | bash": (0, "MNGR_SLICE_RESERVED 22001 22002\n", ""),
            "limactl --log-level=info start": (0, "", ""),
        }
    )
    result = client.provision_slice_vm(
        host_id=host_id,
        env_name="dev-josh",
        vcpus=2,
        memory_mib=8192,
        disk_gib=40,
        host_dir="/mngr",
        root_authorized_public_key="ssh-ed25519 AAAA",
        host_private_key_pem="PEM",
        host_public_key_openssh="ssh-ed25519 BBBB",
        boot_disk_gib=32,
        slot_count=6,
        port_range_start=22000,
        port_range_end=32000,
    )
    # The ports come from the box reservation, and the instance/disk names are env-stamped.
    assert result.vm_ssh_host_port == 22001
    assert result.container_ssh_host_port == 22002
    assert result.instance_name == f"mngr-slice-dev-josh-{host_id.get_uuid().hex}"
    assert result.disk_name == f"mngr-slice-dev-josh-{host_id.get_uuid().hex}-data"
    # The reserve happened before the boot, and the boot was a separate command.
    reserve_idx = next(i for i, cmd in enumerate(client.recorded_commands) if "base64 -d | bash" in cmd)
    start_idx = next(i for i, cmd in enumerate(client.recorded_commands) if "limactl --log-level=info start" in cmd)
    assert reserve_idx < start_idx


def test_provision_slice_vm_raises_slice_capacity_error_when_box_is_full() -> None:
    client = _recording_client({"base64 -d | bash": (4, "", "MNGR_SLICE_BOX_FULL 6/6")})
    with pytest.raises(SliceCapacityError):
        client.provision_slice_vm(
            host_id=HostId.generate(),
            env_name="dev-josh",
            vcpus=2,
            memory_mib=8192,
            disk_gib=40,
            host_dir="/mngr",
            root_authorized_public_key="ssh-ed25519 AAAA",
            host_private_key_pem="PEM",
            host_public_key_openssh="ssh-ed25519 BBBB",
            boot_disk_gib=32,
            slot_count=6,
            port_range_start=22000,
            port_range_end=32000,
        )
