Added a dedicated permission flow for the cross-workspace management API (`/api/v1/workspaces/...`), alongside the existing `predefined` (service-catalog) and `file-sharing` permission types.

- Introduced a `WORKSPACE_PERMISSION` request type and a `LatchkeyWorkspacePermissionRequestEvent`: an agent that hits a 403 on a cross-workspace call files a `type=workspace` request naming the `minds-workspaces` verbs it wants and, for verbs that act on a specific workspace, the target workspace id.

- Added a `WorkspacePermissionGrantHandler` and its inbox dialog (`LatchkeyWorkspacePermission`): the user picks which verbs to grant (read / create / destroy / lifecycle / backups-export / ssh) and, when the request names a target workspace, whether the target-scoped verbs apply to only that workspace or to all workspaces. The grant is applied through the gateway's standard approve endpoint (with a recompute-on-approval override body), exactly like file-sharing -- the desktop client never writes the permissions file directly, and no host resolution is needed.

- The `read` and `create` verbs are all-or-nothing; the `destroy`, `lifecycle`, `backups-export`, and `ssh` verbs are target-scoped and accumulate one approved target workspace at a time (each becomes a uniquely-named per-target schema), so granting access to another workspace adds to the set rather than replacing it.

- Updated `apps/minds/docs/latchkey-permissions.md` with a "Cross-workspace management API permissions" section describing the verbs, the target axis, and the self-contained grant effect.
