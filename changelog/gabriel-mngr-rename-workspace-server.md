Renamed the mngr-side "workspace server" feature to "system interface",
matching the upstream rename of the `minds_workspace_server` package to
`system_interface` in `forever-claude-template`. The HTTP endpoint
`/api/agents/{id}/restart-workspace-server` became
`/api/agents/{id}/restart-system-interface`, the SSE event type
`workspace_server_status` became `system_interface_status`, the menu
item / recovery page label "Restart workspace server" became "Restart
system interface", and the plugin's 503 loader page now reads "System
interface starting". The `mngr_forward` envelope contract was renamed
in lockstep: `WorkspaceBackendFailure*` → `SystemInterfaceBackendFailure*`
and the envelope type literal `workspace_backend_failure` →
`system_interface_backend_failure`. Frontend Electron clients
automatically pick up the new wire format and labels.
