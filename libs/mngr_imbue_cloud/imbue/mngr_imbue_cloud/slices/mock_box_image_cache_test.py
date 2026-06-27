from pydantic import Field

from imbue.mngr_imbue_cloud.errors import BoxImageCacheError
from imbue.mngr_imbue_cloud.slices.box_image_cache import BoxImageCacheInterface
from imbue.mngr_imbue_cloud.slices.box_image_cache import TransferKey


class MockBoxImageCache(BoxImageCacheInterface):
    """In-memory BoxImageCache for unit-testing the slice provider's seed/load orchestration."""

    tars_present: set[str] = Field(default_factory=set, description="image_tags whose tar 'exists' on the box")
    locks_held: set[str] = Field(default_factory=set, description="image_tags currently locked")
    free_bytes: int = Field(default=10**12, description="Simulated free disk on the box")
    saved_tags: list[str] = Field(default_factory=list, description="image_tags saved, in order")
    loaded_tags: list[str] = Field(default_factory=list, description="image_tags loaded, in order")
    created_keys: list[TransferKey] = Field(default_factory=list, description="Ephemeral keys created, in order")
    destroyed_keys: list[TransferKey] = Field(default_factory=list, description="Ephemeral keys destroyed, in order")
    is_save_failing: bool = Field(default=False, description="Whether save_image_from_slice raises")
    is_load_failing: bool = Field(default=False, description="Whether load_image_into_slice raises")
    is_tar_published_on_wait: bool = Field(
        default=False, description="When set, wait_for_tar publishes the tar (simulates an in-flight seed finishing)"
    )

    def has_tar(self, image_tag: str) -> bool:
        return image_tag in self.tars_present

    def try_acquire_build_lock(self, image_tag: str) -> bool:
        if image_tag in self.locks_held:
            return False
        self.locks_held.add(image_tag)
        return True

    def release_build_lock(self, image_tag: str) -> None:
        self.locks_held.discard(image_tag)

    def wait_for_tar(self, image_tag: str, *, timeout_seconds: int) -> bool:
        if self.is_tar_published_on_wait:
            self.tars_present.add(image_tag)
        return image_tag in self.tars_present

    def check_free_disk(self, required_bytes: int) -> None:
        if self.free_bytes < required_bytes:
            raise BoxImageCacheError(f"insufficient disk: need {required_bytes}, have {self.free_bytes}")

    def create_transfer_key(self) -> TransferKey:
        transfer_key = TransferKey(
            private_key_path_on_box=f"/box/.transfer-{len(self.created_keys)}",
            public_key=f"ssh-ed25519 MOCKKEY{len(self.created_keys)}",
        )
        self.created_keys.append(transfer_key)
        return transfer_key

    def destroy_transfer_key(self, transfer_key: TransferKey) -> None:
        self.destroyed_keys.append(transfer_key)

    def save_image_from_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        if self.is_save_failing:
            raise BoxImageCacheError(f"mock save failure for {image_tag}")
        self.saved_tags.append(image_tag)
        self.tars_present.add(image_tag)

    def load_image_into_slice(self, image_tag: str, *, vm_ssh_port: int, transfer_key: TransferKey) -> None:
        if self.is_load_failing:
            raise BoxImageCacheError(f"mock load failure for {image_tag}")
        self.loaded_tags.append(image_tag)
