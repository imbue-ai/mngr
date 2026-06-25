Begin work on the versioned Minds workspace API (cross-workspace capabilities reachable by agents through the latchkey gateway). See `blueprint/minds-workspace-api/plan-minds-workspace-api.md` for the full design.

- Added `restic_cli.list_snapshots` plus a `ResticSnapshot` data type and a `parse_restic_snapshots` parser, so a workspace's backup snapshots can be enumerated (not just the latest). This is the foundation for backup listing and per-snapshot export in the new API.

- Fixed the permission-grant flow to treat an `UNKNOWN` latchkey credential status as "proceed" rather than "needs credential setup". Only `MISSING`/`INVALID` now trigger the browser-auth or manual-credentials path. This stops the dialog from spuriously prompting for credentials when latchkey cannot vouch for a credential it does not manage (e.g. a `rawCurl` credential, or the minds-internal API scopes served by a gateway extension), and is a prerequisite for granting those scopes.
