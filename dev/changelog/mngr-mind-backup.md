Added spec `specs/host-backup/concise.md` for a new continuous-backup
service that runs inside every mind workspace. The service uses restic
against a Cloudflare R2 bucket by default and takes consistent btrfs
subvolume snapshots on lima / vps-docker (no-op on plain docker). The
in-container `host_backup` library + bootstrap config wiring lives in
forever-claude-template (separate PR). This monorepo's changes provision
the outer-side `snapshot_helper.sh` systemd unit on vps-docker hosts;
see `libs/mngr_vps_docker/changelog/mngr-mind-backup.md` and
`libs/mngr_ovh/changelog/mngr-mind-backup.md` for the per-project
details.
