Added per-target permission grants for the cross-workspace `minds-workspaces` API.

- New `workspace_permissions` module: the `minds-workspaces` verb catalog (read / create / destroy / lifecycle / backups-export / ssh), the non-targeted baseline schemas, and `grant_workspace_permissions`, which applies a user-approved grant by unioning the verbs into the host's `minds-workspaces` rule and -- for the target-scoped verbs -- accumulating the approved target workspace id into each verb's permission schema as an `anyOf` of path patterns (generalizing the per-agent `minds-api-proxy` allowlist machinery).

- The `permission-requests` gateway extension now accepts a third request type, `workspace` (alongside `predefined` and `file-sharing`), validating the requested verbs against the `minds-workspaces` verb set and the optional `target_workspace_id` as an agent id.

- The agent baseline and the `minds-workspaces` startup schema migration now materialize only the scope gate plus the broad `read`/`create` verb schemas; the target-scoped verb schemas are created on first grant (with a non-empty `anyOf`), keeping the deny-by-default baseline free of an empty `anyOf`.
