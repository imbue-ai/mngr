from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.slices.qemu_slice_client import QemuSliceVpsClient
from imbue.mngr_imbue_cloud.slices.slice_client import build_slice_vm_client


def test_factory_dispatches_on_the_box_generation() -> None:
    gen1_client = build_slice_vm_client(
        box_generation=1,
        box_address="203.0.113.1",
        box_ssh_port=22,
        box_ssh_user="slicehost",
        private_key_path=None,
        box_host_public_key=None,
    )
    assert isinstance(gen1_client, LimaSliceVpsClient)
    gen2_client = build_slice_vm_client(
        box_generation=2,
        box_address="203.0.113.1",
        box_ssh_port=22,
        box_ssh_user="slicehost",
        private_key_path=None,
        box_host_public_key=None,
    )
    assert isinstance(gen2_client, QemuSliceVpsClient)
