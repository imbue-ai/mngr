Workspace-server restart and health-recovery support on the `mngr_forward` plugin (consumed by minds).

- The plugin emits `workspace_backend_failure` envelopes when it sees connection errors, mid-SSE EOF, or 5xx responses from the workspace backend. Consumers (minds) can track these as a per-agent health state machine to trigger a recovery UI.
- The plugin's 503 fallback page (shown while the workspace server is
  unreachable) is now a styled card with a loading spinner instead of the
  blank "Backend not yet available. Retrying..." page. It still auto-refreshes
  every second.
- The "Workspace server starting" loader spinner's animation duration now
  matches the page's 1-second auto-refresh interval, so the spinner is at
  the cycle boundary (rather than 90 degrees past it) when the reload fires.
