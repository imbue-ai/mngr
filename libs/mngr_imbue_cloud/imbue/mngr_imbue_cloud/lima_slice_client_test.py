import pytest

from imbue.mngr_imbue_cloud.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.lima_slice_client import parse_listening_ports
from imbue.mngr_lima.errors import LimaCommandError
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# Note: provision_slice_vm / destroy_instance / get_instance_status drive limactl
# over SSH against a real box and are exercised by the live slice smoke test, not
# here. These unit tests pin the parts that need no box: the interface contract,
# the SSH command construction, and the listening-port parsing.


def _client() -> LimaSliceVpsClient:
    return LimaSliceVpsClient(box_address="box.example", box_ssh_user="limahost", private_key_path="/tmp/id")


def test_cloud_only_operations_raise_not_implemented() -> None:
    client = _client()
    with pytest.raises(NotImplementedError):
        client.create_instance("label", "region", "plan", "", (), {})
    with pytest.raises(NotImplementedError):
        client.create_snapshot(VpsInstanceId("mngr-slice-x"), "desc")
    with pytest.raises(NotImplementedError):
        client.list_snapshots()
    with pytest.raises(NotImplementedError):
        client.upload_ssh_key("name", "ssh-ed25519 AAAA")
    with pytest.raises(NotImplementedError):
        client.list_ssh_keys()


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
    # The remote command is the last arg, prefixed with an explicit PATH so a
    # non-login shell still finds limactl (extracted to /usr/local/bin by prep).
    assert command[-1].endswith("limactl list --json")
    assert "/usr/local/bin" in command[-1]


def test_box_ssh_command_requires_a_private_key() -> None:
    client = LimaSliceVpsClient(box_address="box.example", box_ssh_user="limahost", private_key_path=None)
    with pytest.raises(LimaCommandError):
        client._box_ssh_command("limactl list --json")


def test_parse_listening_ports_extracts_ipv4_ipv6_and_wildcard() -> None:
    ss_output = (
        "LISTEN 0      128          0.0.0.0:22         0.0.0.0:*\n"
        "LISTEN 0      128             [::]:22            [::]:*\n"
        "LISTEN 0      128       127.0.0.1:5432       0.0.0.0:*\n"
        "LISTEN 0      128                *:22001              *:*\n"
        "garbage line with too few fields\n"
    )
    assert parse_listening_ports(ss_output) == {22, 5432, 22001}
