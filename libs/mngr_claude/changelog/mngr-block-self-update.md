Added an `update_policy` field to the claude agent type that governs Claude Code's background auto-updater. `NEVER` sets `DISABLE_AUTOUPDATER=1` in the agent environment so the installed (optionally `version`-pinned) binary stays put; `AUTO` leaves the auto-updater enabled; `ASK` behaves like `AUTO` (claude has no interactive update flow). When unset, it resolves to `NEVER` for unattended (remote/deploy) agents and `AUTO` for attended local agents.

Pin a specific version with `version` together with `update_policy = "NEVER"` to keep claude frozen on that version.
