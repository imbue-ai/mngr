Gen-2 storage encryption support in the slice backend.

- The `mngr-slice@` unit template gains `RequiresMountsFor=/srv/mngr-slices`, so a slice VM never boots against an absent (locked) storage volume.

- New layout constants for the LUKS storage volume (`GEN2_STORAGE_LUKS_MAPPER_NAME` / `_PATH`, `GEN2_STORAGE_SYSTEM_DIR`), a storage-volume probe (`build_read_storage_volume_command` / `parse_storage_volume_output` into the new `StorageVolumeState`), and `read_storage_volume_state()` on the slice VM client interface (qemu, lima, and the mock). `BoxTierAudit` carries `is_storage_encrypted`.
