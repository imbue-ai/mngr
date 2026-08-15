Workspace-sync SSH key correctness fixes (Phase 0 of the user-controlled-keys plan, `blueprint/user-controlled-keys/`):

- Synced known_hosts material now replaces the existing pin for the same `[host]:port` and keytype instead of appending behind it. Previously a stale pin ordered first permanently shadowed the live key (paramiko takes the first entry of a keytype), which could wedge a synced workspace on a new machine forever.

- The decrypted workspace-secrets payload now tolerates unknown fields, so a future minds version can add payload fields without making older installs reject the entire blob (which would have cost them the DR-critical restic env too).
