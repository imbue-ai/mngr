Adds the `minds-workspaces` detent permission scope used by the minds cross-workspace management API, with one named permission per verb (`minds-workspaces-read`, `-create`, `-destroy`, `-lifecycle`, `-backups-export`, `-ssh`, `-update`, `-recover`, `-sharing`).

- Verbs split on a target axis: `read`/`create` are all-or-nothing, while `destroy`/`lifecycle`/`backups-export`/`ssh`/`update`/`recover`/`sharing` are target-scoped. A "selected" grant mints a uniquely-named per-target permission schema (`minds-workspaces-<verb>-<target_id>`) that pins a single workspace; an "all workspaces" grant uses the broad verb schema. Successive selected grants accumulate targets through the gateway's ordinary schema-by-name merge.

- A verb's `method` in the shared catalog may now be a single HTTP method or an array of methods; multi-method verbs (e.g. `recover` matches `GET`/`POST`, `sharing` matches `GET`/`PUT`/`DELETE`) produce a permission schema whose `method` is a JSON-Schema `enum`.

- The grant is applied like file-sharing: the agent's `type=workspace` permission request carries a precomputed effect (scope schema + verb schemas + rule, built in `permission_requests.mjs`'s `computeWorkspaceEffect`), spliced into the requesting agent's per-host permissions file on approval. The Python side keeps only the dialog-facing verb metadata.
