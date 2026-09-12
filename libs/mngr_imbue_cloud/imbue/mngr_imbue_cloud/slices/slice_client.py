"""The one place a box's slice-fleet generation is turned into a slice VM client."""

from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_imbue_cloud.slices.qemu_slice_client import QemuSliceVpsClient


def build_slice_vm_client(
    *,
    box_generation: int,
    box_address: str,
    box_ssh_port: int,
    box_ssh_user: str,
    private_key_path: str | None,
    box_host_public_key: str | None,
    gen1_vm_image_url: str | None = None,
) -> SliceVmClientInterface:
    """Build the slice VM client the box's generation calls for.

    ``gen1_vm_image_url`` overrides the lima guest image and is meaningless to
    the gen-2 client (which boots every slice from the box-staged base image),
    so it is ignored there.
    """
    if box_generation >= FIRST_QEMU_BOX_GENERATION:
        return QemuSliceVpsClient(
            box_address=box_address,
            box_ssh_port=box_ssh_port,
            box_ssh_user=box_ssh_user,
            private_key_path=private_key_path,
            box_host_public_key=box_host_public_key,
        )
    return LimaSliceVpsClient(
        box_address=box_address,
        box_ssh_port=box_ssh_port,
        box_ssh_user=box_ssh_user,
        private_key_path=private_key_path,
        vm_image_url=gen1_vm_image_url,
        box_host_public_key=box_host_public_key,
    )
