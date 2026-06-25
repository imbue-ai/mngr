Begin work on the versioned Minds workspace API (cross-workspace capabilities reachable by agents through the latchkey gateway). See `blueprint/minds-workspace-api/plan-minds-workspace-api.md` for the full design.

- Added `restic_cli.list_snapshots` plus a `ResticSnapshot` data type and a `parse_restic_snapshots` parser, so a workspace's backup snapshots can be enumerated (not just the latest). This is the foundation for backup listing and per-snapshot export in the new API.
