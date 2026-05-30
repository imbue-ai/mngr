Provisioned a per-host outer-side btrfs snapshot helper for the new
forever-claude-template `host_backup` service. Each vps-docker host now
gets:

- `/usr/local/sbin/snapshot_helper.sh` + `snapshot_helper.service` (a
  systemd unit shipped as a bundled resource in
  `imbue/mngr_vps_docker/resources/`) that watches a per-host docker
  volume `mngr-snapshot-trigger-<host_id_hex>` for `request.json` files
  and produces matching `result.json` files describing the outcome of
  `btrfs subvolume snapshot` / `btrfs subvolume delete` against the
  per-host subvolume.
- That docker volume is mounted into the agent container at
  `/mngr-snapshot/` so the in-container `host_backup` script can do the
  RPC; the outer's `<btrfs-mount>/snapshots/` directory is bind-mounted
  read-only into the container at `/mngr-snapshots/` so restic can read
  the snapshot the helper produced.
- Cloud-init now installs `inotify-tools` and `jq` so the helper has
  what it needs at boot.
- `destroy_host` removes the per-host snapshot-trigger volume alongside
  the existing host-volume cleanup.
