Added spec `specs/host-backup/concise.md` for a new continuous-backup service
that runs inside every mind workspace. The service uses restic against a
Cloudflare R2 bucket by default and takes consistent btrfs subvolume snapshots
on lima / vps-docker (no-op on plain docker). Implementation lives in a new
`libs/host_backup/` library in forever-claude-template plus an outer
`snapshot_helper.sh` systemd unit shipped via `libs/mngr_vps_docker` cloud-init
(this monorepo's changes will follow in a separate PR per project).
