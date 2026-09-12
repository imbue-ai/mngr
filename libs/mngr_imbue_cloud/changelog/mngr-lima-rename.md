Generic names for the slice identifiers that survive the gen-2 cutover (imbue-ai/mngr-internal#848).

- The gen-2 box service user is `slicehost` (`GEN2_SLICE_SERVICE_USER`); `SliceVpsDockerProviderConfig.box_ssh_user` defaults to it. Gen-1 boxes keep `limahost` (`GEN1_SLICE_SERVICE_USER`, deleted with the gen-1 code in cutover phase 6).

- `slice_lima_instance_name` / `slice_lima_disk_name` / `SLICE_LIMA_INSTANCE_PREFIX` / `SLICE_LIMA_DISK_SUFFIX` are `slice_instance_name` / `slice_disk_name` / `SLICE_INSTANCE_PREFIX` / `SLICE_DISK_SUFFIX`: the `mngr-slice-<env>-<hex>` names are shared by both generations.

- `BareMetalServer.lima_service_user` is `slice_service_user`; `PoolHostDestroyTarget.lima_instance_name` / `lima_service_user` are `slice_instance_name` / `slice_service_user`. New `default_slice_service_user` and `box_service_user` helpers resolve a box's user from its row, else its generation's default.
