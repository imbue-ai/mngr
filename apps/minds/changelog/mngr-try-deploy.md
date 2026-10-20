Changed: documented (in `docs/next_deploy.md`) the old-workspace terminal
regression found during the dev-josh-1 deployment rehearsal: pre-update
workspaces' system_interface serves terminal/browser panels through a
`/service/<name>/` service-worker bootstrap that loops forever under the new
desktop client's partitioned content embedding. The fix is a compat redirect
in the forward proxy (see `libs/mngr_forward`'s changelog entry); old
workspaces now keep working terminals without `update-self`.
