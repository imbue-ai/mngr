# Tiered restart and unified health-recovery UX

## Overview

- The current branch handles one failure mode — workspace-server (system interface) wedged — with one fix: surgical `tmux kill-window` + `touch services.toml`. Some failures need a deeper fix (container itself is sick), and today there's no in-app way to do that.
- Add a sibling **container restart** tier alongside the existing surgical tier. Container restart is in-app and works for both local and remote agents through the existing `ProviderInstance` abstraction. The user never has to leave the app to recover.
- Pick the right tier from probes (not from a user-facing tier picker). A new **layer-2 probe** observes container-level signals (ttyd loopback, `docker inspect`, bootstrap tmux window) so the recovery flow can route silently to surgical when the container is healthy and route directly to container restart when it isn't.
- Replace the existing chrome banner with a modal-driven flow. Auto-recovery for surgical (non-destructive) is silent and reassuring ("your agents are unaffected"); escalation to container restart (destructive) always confirms.
- Rename user-facing copy from "workspace server" to "system interface" to match the new path in `forever-claude-template/apps/system_interface/`. Internal identifiers stay as-is unless trivially co-renamed.
- Both restart endpoints switch to 202-immediate + SSE-driven UI to avoid long-blocking HTTP requests for container restart (which can take 30-60s).

## Expected behavior

### Auto-recovery (tracker enters STUCK)

- If the layer-2 probe says the container is healthy, minds auto-fires the surgical restart and shows a non-dismissable modal: *"System interface got stuck. Reloading (your agents are unaffected)."*
- The modal stays visible until SSE reports HEALTHY (reload back to the workspace) or 15 seconds pass without recovery.
- After 15 seconds, or if the layer-2 probe initially showed a container-level fault, the modal switches to a confirmation prompt: *"Workspace seems stuck. Would you like to restart it? (In-progress work may be interrupted.)"* The user clicks **Restart** to trigger container restart, or **Cancel** to dismiss.
- Once container restart is in flight, the modal becomes non-dismissable and reads *"Restarting workspace…"* until SSE reports HEALTHY.

### Manual restart affordances

- The home page gains a restart icon per workspace row. Clicking it opens the confirmation modal ("Restart workspace? In-progress work may be interrupted.") and on confirm fires container restart.
- The sidebar right-click menu exposes two entries:
  - **Restart system interface** — no confirmation, fires surgical restart immediately. If recovery doesn't arrive within 15 seconds, the confirmation modal for container restart pops up.
  - **Restart workspace** — same confirmation modal as the home page; fires container restart on confirm.

### Failure handling

- If a restart dispatch fails (SSH unreachable, docker SDK error), the tracker enters a new `RESTART_FAILED` state. The modal surfaces the reason: *"Restart failed: <reason>. Try again?"* with a retry button.
- A subsequent probe success at any point flips the tracker back to HEALTHY and clears the modal.

### Direct navigation fallback

- The plugin's existing 503 → 302 redirect path continues to work: it lands on `/agents/{id}/recovery`, which renders the same modal-driven UI (status, confirmation prompts, RESTART_FAILED handling) as the chrome modal — so direct-link entry behaves identically to in-app recovery.

### What stays the same

- The plugin's role: it remains a reverse proxy that emits `workspace_backend_failure` envelopes on connect errors, mid-SSE EOF, and 5xx responses. It does not gain probing or interpretation responsibilities.
- The tracker's state-transition rules (HEALTHY → STUCK after continuous failures → RESTARTING during a user-triggered restart → HEALTHY on success) are unchanged. `RESTART_FAILED` is an additional state, not a replacement.
- Existing surgical restart mechanism: `tmux kill-window` + `touch services.toml`, dispatched via `mngr exec`.

## Changes

### New: container-restart tier

- A new minds endpoint `POST /api/agents/{id}/restart-container` that returns 202 immediately, marks the tracker `RESTARTING`, and asynchronously calls into the existing provider abstraction to stop and start the container. Works for local + remote agents through the same provider path.
- The existing `POST /api/agents/{id}/restart-workspace-server` migrates to the same 202-immediate pattern for consistency. Dispatch failures from either endpoint propagate to the tracker as `RESTART_FAILED` and are broadcast over SSE.

### New: layer-2 probe

- A background probe in minds that, while any agent is non-HEALTHY, queries that agent's container-level signals every 2-3 seconds: ttyd loopback reachability (via `mngr exec` into the container), `docker inspect` running/healthy state, presence of the bootstrap tmux window. Probe is idle when all tracked agents are HEALTHY.
- The probe runs on layer-1 failure too — when the tracker first sees a failure envelope, it triggers a one-shot layer-2 probe so the recovery modal can pick the right initial tier without waiting for the next periodic poll.

### Tracker

- Add a `RESTART_FAILED` state and a `last_restart_error` field carried with it. SSE messages for that state include the error string so the modal can render it.
- Feed layer-2 probe failures into the tracker as an additional STUCK-driving signal (today only L1 envelopes drive STUCK).

### Frontend

- Remove the chrome banner. Add a modal overlay component on the chrome page that subscribes to SSE and renders the auto-recovery, confirmation, in-flight, and RESTART_FAILED states described above.
- Add a restart icon to each workspace row on the home page; wire it to the confirmation modal.
- Add two entries to the sidebar context menu: "Restart system interface" (immediate) and "Restart workspace" (confirm). The "Restart system interface" entry tracks the response and pops up the confirmation modal if recovery doesn't arrive within 15 seconds.
- Update the existing `/agents/{id}/recovery` page to mirror the modal's state machine so direct-navigation users see the same flow.
- Rename user-facing strings from "workspace server" to "system interface" across banners, modals, menus, and the recovery page.

### What is explicitly out of scope for v1

- No "restart the agent" middle tier (no `mngr stop` + `mngr start` path).
- No agent-initiated workspace-server restart flow; no `request-workspace-server-restart` skill in `forever-claude-template`.
- No per-agent busy detection. Confirmation modals always show the "in-progress work may be interrupted" warning unconditionally.
