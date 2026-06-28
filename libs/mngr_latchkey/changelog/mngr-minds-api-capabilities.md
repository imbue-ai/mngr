Added the `minds-workspaces` detent scope and its named permissions (`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`) to the per-agent latchkey permissions baseline. This gates the minds desktop client's new cross-workspace management API (`/minds-api-proxy/api/v1/workspaces/...`): the scope is materialized in every per-host permissions file but not pre-granted, so an agent goes through the standard permission-request dialog before its first cross-workspace call and the user picks which verbs to allow.

- `ensure_minds_workspaces_schema_in_existing_host_files` backfills the scope + permission schemas into permissions files created before the scope shipped (run at `minds run` startup, before the gateway restarts).

- `store.list_host_permissions_paths` enumerates the per-host permissions files (used by the migration).

- The `services.json` generator now preserves manually-curated, non-detent scope entries (like `minds-workspaces`) across regenerations.

- The `minds-workspaces-ssh` permission now pins its HTTP method to `POST`, matching the other write verbs (`-create`/`-destroy`/`-lifecycle`/`-backups-export`) and the scope's one-verb-per-permission convention.
