Minds: fix "Open in new window" navigating to a 404. The Electron `workspaceUrlForAgent` now targets the `mngr_forward` plugin (which owns subdomain forwarding and `/goto/`), not the minds backend.
