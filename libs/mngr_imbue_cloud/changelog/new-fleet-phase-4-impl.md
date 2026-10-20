Phase 4 of the slice-fleet cutover: gen-2 disk accounting and the restore-reserve options the cutover's restore needs.

- Gen-2 sizing: `compute_gen2_disk_budget_gib(storage_partition_gib)` (the measured storage partition minus a 64 GiB reserve for the swapfile, image tar cache, base image and staging margin) and `compute_gen2_storage_partition_estimate_gib(usable_disk_gb)` (before prep measures it: the disk minus the 20 GiB root and 1 GiB boot partitions) replace `compute_box_disk_budget_gib`; the partition sizes, swapfile, image cache dir and prep marker are named constants in `gen2_scripts`. A gen-2 box's image tar cache lives at `/srv/mngr-slices/image-cache` (`box_image_cache_dir_for_generation`).

- `render_gen2_restore_reserve_script` accepts `fixed_ports=(vm, container)` (claim those exact host ports, refusing with the no-ports marker when either is taken, instead of picking free ones) and `user_data_b64` (a caller-supplied cloud-init user-data instead of the artifact's meta tar). The default rendering is byte-identical to before.

- `ImbueCloudConnectorClient.admin_start_workspace(admin_api_key, host_db_id)` for the connector's new admin start endpoint.
