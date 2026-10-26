- A synced folder now reads "Syncing" until unison's first pass has actually compared the two sides and left them level, and only then "Synced". It used to say "Synced" the moment unison started, before anything had been compared or copied.

- New `GET /api/v1/agents/<agent_id>/folder-syncs`, so an agent in a workspace can read this desktop's folder syncs with it: the desktop's device id, and per folder its local path, its workspace path (from the workspace's home, `~/...`), whether it is on, its state, transfer progress, direction, and conflict rule. Any agent of the workspace may ask under its own id; it is reachable through the gateway's existing per-agent rule.

- The Permissions pane's once-a-second poll no longer turns a sync that failed to come back at launch into a plain "stopped" row while another sync is running; it reports the failure, as the full view does.

- A workspace app can now ask Imbue Studio to open the Permissions pane's Local files section, where the user shares files and folders from this computer and turns syncing on (embed contract v11). The chrome says it takes the ask with the new `opensLocalFiles: true` on `minds:embedder-capabilities`; the workspace sends `minds:open-local-files`.

- Adding a path that is already shared, from the Permissions pane's Add buttons, leaves it as it is. It used to re-grade it to read-only, taking away write access and turning a two-way sync one-way.
