# Stopped cloud workspaces are no longer dead-end "on <device>" tiles

A cloud (Imbue Cloud) workspace that this device's discovery cannot currently see -- most visibly a stopped one -- used to render on the Machines list as a greyed remote tile badged "on <hostname>" (the hostname of whichever device happened to create it), with no chip explaining why and no way to reach its backups.

- The tile's badge now names the provider ("Imbue Cloud") for cloud records; only machines hosted by another install are badged "on <device>". The wire entry carries the distinction as `remote_kind` (`cloud` / `other_device`).

- When the account's Imbue Cloud provider is disabled ("Signed out" on the Accounts page), the tile now shows a "Signed out" chip that links to the Accounts page instead of silently suppressing every chip.

- Remote tiles and stopped machines get a small Backups button that opens the existing per-machine backups page (`/workspace/<id>/backups`), which runs restic from this device and so works without the machine being reachable. The button is disabled with an explanation when the credentials are locked behind the master password on this device, or never synced here (no master password set on the device that created the machine). The wire entry carries this as `backup_access`.

- `UI_SCHEMA_VERSION` bumped to 7 for the two new `UiWorkspaceEntry` fields.
