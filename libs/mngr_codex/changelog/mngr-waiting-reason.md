Codex agents now report *why* they are waiting, via a `waiting_reason` field in `mngr list` (matching `mngr_claude`):

- `PERMISSIONS` -- the agent is blocked on a tool-approval dialog. A `PermissionRequest` hook touches a `permissions_waiting` marker, and the agent's lifecycle state now reports WAITING (not RUNNING) while the dialog is open. `PostToolUse` clears the marker once the approved tool runs, and the root `Stop` clears any stranded marker as a safety net.

- `END_OF_TURN` -- the agent is idle with its turn complete.

This applies only in supervised mode; with `auto_allow_permissions = true` codex never prompts, so a permission reason never appears. Verified live against codex 0.139.0.
