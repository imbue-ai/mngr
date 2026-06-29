import re
from abc import ABC
from abc import abstractmethod
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# How long a seed build may hold the per-box build lock before its marker is
# considered stale and reclaimable (matches the inner ``mngr create`` budget, so a
# builder that died mid-seed does not wedge the box's pool fill).
BUILD_LOCK_TTL_SECONDS: Final[int] = 1800
# How long a non-builder bake waits for the in-flight seed to publish the tar.
WAIT_FOR_TAR_TIMEOUT_SECONDS: Final[int] = 1800


class TransferKey(FrozenModel):
    """A unique ephemeral SSH keypair generated on the box for a single image transfer."""

    private_key_path_on_box: str = Field(description="Path of the ephemeral private key on the box")
    public_key: str = Field(description="OpenSSH public key to authorize on the slice's VM root for the transfer")


def box_image_tar_name(image_tag: str) -> str:
    """Filesystem-safe tar filename for an image ref (``fct:minds-v0.3.2`` -> ``fct-minds-v0.3.2.tar``)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", image_tag).strip("-")
    return f"{safe}.tar"


class BoxImageCacheInterface(MutableModel, ABC):
    """Manages the single cached FCT image tar a bare-metal box keeps to skip per-slice image builds."""

    @abstractmethod
    def has_tar(self, image_tag: str) -> bool:
        """Return whether the box already holds a saved image tar for image_tag."""

    @abstractmethod
    def try_acquire_build_lock(self, image_tag: str) -> bool:
        """Atomically become the seed builder for image_tag (reclaiming a stale lock); True if acquired."""

    @abstractmethod
    def release_build_lock(self, image_tag: str) -> None:
        """Release the seed build lock for image_tag (no-op if not held)."""

    @abstractmethod
    def wait_for_tar(self, image_tag: str, *, timeout_seconds: int) -> bool:
        """Block until the tar for image_tag exists; True if it appeared, False on timeout."""

    @abstractmethod
    def check_free_disk(self, required_bytes: int) -> None:
        """Raise BoxImageCacheError if the box has less than required_bytes free for the tar."""

    @abstractmethod
    def create_transfer_key(self) -> TransferKey:
        """Generate a unique ephemeral keypair on the box for one save/load transfer."""

    @abstractmethod
    def destroy_transfer_key(self, transfer_key: TransferKey) -> None:
        """Remove the ephemeral private key from the box (idempotent)."""

    @abstractmethod
    def save_image_from_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        """docker save image_tag from the slice into the box tar atomically (box-local), then prune other tags."""

    @abstractmethod
    def load_image_into_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        """docker load the box's cached tar for image_tag into the slice's dockerd (box-local)."""
