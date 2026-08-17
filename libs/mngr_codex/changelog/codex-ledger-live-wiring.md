The codex app-server client (`CodexAppServerClient`) is now safe for one persistent connection shared between a background notification reader and live request threads: a reentrant frame-level lock serializes `poll_notifications`, `_request`, and `_notify` so the reader and a concurrent send/interrupt never steal each other's frames. This is what lets the system interface give each codex agent one long-lived connection driving its message ledger.

Removed the retired stop-retract idle machinery now that the system interface interrupts codex through its live ledger rather than a control file: `mark_codex_agent_idle`, the `codex_marker_state.sh` marker helper (and its provisioning), and the legacy `permissions_waiting` marker name.

The codex release end-to-end test now observes RUNNING from the daemon's live `thread/status` (a turn in flight is `active`), matching how the live lifecycle reads it, since codex no longer writes an `active` marker.
