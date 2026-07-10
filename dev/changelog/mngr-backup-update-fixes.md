# Backup-update-fixes spec and snapshot-script docs

- Added `specs/backup-update-fixes/concise.md`, the plan for the per-workspace backup health route, the master-password hash + rotation flow, the fixed minimum backup version, the `official` remote, and the snapshot-resume test rewrite.

- `scripts/snapshot_minds_e2e_state.py`'s docs no longer hardcode stale snapshot image ids or describe the script as a one-off prototype: it is documented as the standing producer for the `build-minds-snapshot` CI stage, with instructions for minting an image id manually and running individual tests against it via `just test-offload-minds-snapshot <image-id> '--filter <test_name>'`.
