`mngr stop` / `mngr start` on imbue_cloud workspaces now stop and start the whole slice VM: stop gracefully halts the container, then the connector uploads the VM's disks to the tier's storage bucket and frees the bare-metal slot; start restores the workspace (near-instantly within the retention window, or onto any same-region box after it) and the client re-resolves coordinates and re-pins host keys automatically.

Discovery now consumes the connector's `GET /workspaces` (with fallback to the deprecated `/hosts` on old connectors), so stopped/stopping/starting/crashed workspaces stay visible in `mngr list` as STOPPED/STARTING/CRASHED offline hosts and can be resolved by `mngr start`.

Box prep installs pinned `age` + `s5cmd` for the artifact transfers, and the slice boot autostart now skips VMs carrying the stop-requested marker so a box reboot never resurrects a half-uploaded VM.

New `mngr imbue_cloud admin workspaces abandon <host-db-id> --reason ...` operator escape hatch marks a workspace on a permanently dead box as crashed.
