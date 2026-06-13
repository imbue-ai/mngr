import pytest

from imbue.mngr_imbue_cloud.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_vps_docker.primitives import VpsInstanceId

# Note: provision_slice_vm / destroy_instance / get_instance_status drive limactl
# against a real hypervisor and are exercised by the live slice smoke test, not
# here. These unit tests pin the parts that need no VM: the interface contract.


def test_cloud_only_operations_raise_not_implemented() -> None:
    client = LimaSliceVpsClient()
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


def test_get_instance_ip_is_loopback_for_on_box_baking() -> None:
    client = LimaSliceVpsClient()
    assert client.get_instance_ip(VpsInstanceId("mngr-slice-x")) == "127.0.0.1"
