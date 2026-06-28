Added a third permission-request type, `workspace`, to the `permission-requests` gateway extension (alongside `predefined` and `file-sharing`), for the cross-workspace `minds-workspaces` API.

- The extension validates the requested verbs against the `minds-workspaces` verb set (read / create / destroy / lifecycle / backups-export / ssh) and the optional `target_workspace_id` as an agent id, and computes a self-contained `effect` (the scope schema + per-verb permission schemas + the grant rule), applied via the standard `POST /permission-requests/approve` path exactly like file-sharing.

- The target-scoped verbs (destroy / lifecycle / backups-export / ssh) mint a uniquely-named per-target schema (`minds-workspaces-<verb>-<target_id>`) for a "selected" grant, or a broad schema for an "all workspaces" grant. Successive grants accumulate targets through the gateway's ordinary schema-by-name merge -- no `anyOf` and no special merge logic.

- The approve override body is extended so a `workspace` request can recompute its effect at approval time from the user's dialog choices (`{permissions, target_workspace_id}`), mirroring file-sharing's `{path}` override.

- The `minds-workspaces` scope is no longer part of the agent baseline and is not in the service catalog: it has its own dedicated request type, and its scope + verb schemas arrive self-described in the grant effect. The startup schema-backfill migration for it has been removed, and the now-empty `_MANUAL_SERVICE_ENTRIES` hook in the `services.json` generation script has been dropped.

- The `minds-workspaces` verb catalog now lives in a single shared data file, `extensions/workspace_permissions.json`, read by both the gateway extension (for request-path schema construction) and the Python `workspace_permissions` module (for dialog-facing verb metadata), so the two sides cannot drift.
