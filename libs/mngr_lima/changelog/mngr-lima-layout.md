The lima provider now records, reports, and can change a host's size, and its VMs keep their boot disk from filling.

- `mngr list` reports a lima host's real size as `host.resource`: the `--cpus` / `--memory` its VM was created with (else the instance config's values, else lima's own defaults) and the size of the btrfs data disk that backs its data (the boot disk on the exposed layout), instead of the old 4 CPU / 4 GiB / 100 GiB placeholder. The data-disk size is now recorded on the host record; `host_data_disk_size` is validated as a lima size string.

- New `mngr lima resize HOST [--cpus N] [--memory GIB] [--disk GIB]` rewrites the recorded size. A stopped VM is reconfigured at once (`limactl edit` for CPUs and memory, `limactl disk resize` for the data disk); a running VM only has the size recorded, and `mngr start` always brings the VM up to the recorded size before starting it. A data disk never shrinks, and a host on the exposed bind-mount layout has no data disk to resize.

- Every VM's provisioning now caps the systemd journal at 512 MiB (the same cap the VPS and gen-2 hosts get) and, in the btrfs layout, grows the data filesystem to fill its disk on every boot, so a grown disk is usable on the next start.

- `mngr create`'s start-args help says which disk `--disk` is (the boot disk) and points at `mngr lima resize`.
