The Electron workspace-creation e2e driver (`create_workspace_via_electron`) now
fails fast when creation fails. It previously only waited for the workspace-ready
redirect, so any `mngr create` failure (e.g. an unregistered docker runtime) made
the driver block for the full 10-minute navigation budget before timing out with
an opaque Playwright error. It now races the workspace-ready URL against the
create flow's failure view (`#failure-view`), raising `WorkspaceCreationFailedError`
with the surfaced `#error-message` text the moment creation fails -- turning a
silent 10-minute hang into an immediate, diagnosable failure. This affects only the
e2e test/snapshot path, not the shipped app (which already surfaces the failure
view to users).
