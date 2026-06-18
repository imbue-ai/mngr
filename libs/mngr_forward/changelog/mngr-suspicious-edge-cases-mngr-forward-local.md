Hardened suspicious edge-case handling across the forward plugin:

- Config merge: `[plugins.forward]` no longer silently re-enables a plugin that a lower config layer disabled. The plugin now inherits the base config-merge semantics (only fields an override layer explicitly set win) instead of a hand-written field-by-field merge.
- A backend URL that cannot be parsed is now treated as loopback (refused) rather than dialed, closing a fail-open gap in the no-tunnel safety guard.
- `mngr list` snapshot parsing now logs (instead of silently dropping) agent rows that are malformed or missing their id, so a bad upstream payload is visible rather than surfacing only as a confusing "no matching agents" error.
- WebSocket close errors and other previously-silent swallows are now logged at trace level.

No user-facing behavior changes beyond the above; the rest were internal robustness and clarity improvements (removed dead code, removed a thread-race sentinel, made an internal field non-optional).
