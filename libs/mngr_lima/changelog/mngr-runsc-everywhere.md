Switched the default Lima VM image from Ubuntu 24.04 to a pinned Debian 12
"bookworm" genericcloud image (both `aarch64` and `x86_64`). Now that the agent
typically runs inside a Docker container in the VM (`is_host_in_docker`), the VM
only needs Docker + btrfs + sshd, so a lighter base suffices; this also mirrors
the OVH provider's Debian 12 base. The provisioning script is apt-based and works
on Debian unchanged. Override per-arch via `providers.lima.default_image_url_*`.

Format and mount the per-host btrfs data disk in-guest during provisioning,
instead of relying on Lima's guestagent to auto-format it at boot. Minimal cloud
images (the new Debian genericcloud default) ship no `mkfs.btrfs`, so Lima could
not format the `format: true` btrfs additionalDisk -- it left the disk
unformatted and nothing mounted at `/mnt/lima-<name>`, which broke the per-host
subvolume creation (`cannot access '/mnt/lima-...-data'`) in both Lima btrfs
modes (docker-in-VM and direct-in-VM with `is_host_data_volume_exposed=false`).
The provisioning script now installs `btrfs-progs`, formats the data disk if it
is not already btrfs (idempotent; existing snapshot data survives), and mounts it
at the canonical path before the subvolume is created. On later boots Lima's
guestagent handles the mount (`btrfs-progs` now persists in the image).

Added `providers.lima.default_container_run_args` (default empty): extra
arguments appended to the `docker run` that starts the agent container in
`is_host_in_docker` mode. This is the only config path for injecting inner-
container `docker run` flags on Lima (the lima template's `start_arg` maps to
`limactl` VM args, not the container). Pairs with `docker_runtime="runsc"` to run
the agent container under gVisor -- e.g. set it to
`["--workdir=/", "--security-opt=no-new-privileges"]`, the same hardening the
docker provider applies, where `--workdir=/` avoids runsc aborting when the image
WORKDIR (inside the mounted volume) already exists as the process cwd.
