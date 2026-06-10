# Lima provider: run agents directly in the VM (drop docker-in-VM)

Removed the Lima provider's `is_host_in_docker` mode entirely. The Lima provider
no longer runs a Docker daemon, builds an image, or runs the agent inside a
nested container in the VM. Agents always run directly in the Lima VM.

- Added `is_run_as_root` to the Lima provider config. When enabled, mngr runs the
  agent in the VM as root (uid 0) -- so a coding agent can `apt install` and
  write anywhere with no `sudo`, exactly as it can inside a docker/VPS container.
  mngr injects a root client key, enables key-based root login, and SSHes in as
  root.
- `is_run_as_root=true` requires the btrfs additional-disk layout
  (`is_host_data_volume_exposed=false`); the invalid combination with the 9p
  bind-mount layout is rejected at config construction.
- Removed the docker-mode config fields (`is_host_in_docker`, `container_ssh_port`,
  `default_image`, `builder`, `docker_install_timeout`,
  `container_ssh_connect_timeout`, `image_build_timeout_seconds`,
  `default_container_run_args`, `docker_runtime`, `install_gvisor_runtime`).
  Configs that still set them now fail to load.
- Existing docker-mode Lima hosts (records with `is_host_in_docker=true`) are no
  longer startable; destroy and recreate them.
- The Lima provider no longer depends on `mngr_vps_docker`.

Consistent dependency setup across providers is now achieved by having the
project ship idempotent setup scripts that its `Dockerfile` runs (for the
docker/vps_docker/ovh providers) and that the Lima host runs directly after the
project is synced in. btrfs-based backups continue to work because `host_dir`
stays on a btrfs disk and the root agent can snapshot it directly.
