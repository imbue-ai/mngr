# Window Error Watcher

## Overview

A new background service in the **forever-claude-template** (FCT) repo that
periodically scans every window of its tmux session for output matching
`/error|exception/i`. When new matching output appears, it sends a message to a
randomly selected mngr agent so a human-facing agent gets nudged that a service
may have errored.

**Context (resolved during planning).** The "main session" is the forever-claude
agent's tmux session. The bootstrap service manager (`libs/bootstrap/`, the
`uv run bootstrap` process) reads `services.toml` and runs each declared service
in its own tmux window named `svc-<name>`, alongside window 0 (the Claude agent)
and the injected `bootstrap` / `telegram` windows. "A bunch of services in
different windows" is exactly this set of windows. The watcher is implemented as
a **new lib** (`libs/error_watcher/`, mirroring `libs/app_watcher/`) and
registered as a `[services.error-watcher]` entry so the bootstrap manager spawns
it like any other service — this is the "spawned by bootstrap" the user asked
for.

The intent is lightweight, automated detection of error/exception output that
would otherwise scroll past unnoticed, turning it into an actionable nudge.

**Resolved decisions:**

- **Single instance.** One watcher service per session (one `svc-error-watcher`
  window), overseeing all the other windows.
- **Any agent is eligible.** The randomly chosen recipient is any currently
  messageable mngr agent, including the agent that owns the offending window.
- **Lives entirely in FCT.** No change to the monorepo `mngr_forever_claude`
  plugin is required (the plugin injects only the `bootstrap` / `telegram`
  windows; everything else is a `services.toml`-managed service).

## User Scenarios

**Scenario 1 — A service prints a traceback.**
A service running in window `svc-web` hits a Python traceback ending in
`Exception`. On its next poll the watcher captures that window, matches the text,
and sends one message to a randomly chosen agent, e.g. "Possible error/exception
detected in window `svc-web` (session `<name>`): `<matching line>`."
(`REQ-SCAN-1`, `REQ-MATCH-1`, `REQ-NOTIFY-1`, `REQ-NOTIFY-2`)

**Scenario 2 — The same error is still on screen next poll.**
The traceback from Scenario 1 is still visible 15 seconds later. The watcher
does *not* send a second message, because it already alerted on that match.
Only genuinely new matching output triggers a new alert. (`REQ-MATCH-3`)

**Scenario 3 — Multiple windows error in the same poll.**
Two windows show new errors in a single poll. The watcher sends a single
message that summarizes both, to one randomly selected agent, rather than one
message per window. (`REQ-NOTIFY-6`)

**Scenario 4 — No agents are available to message.**
Every agent is STOPPED. The watcher logs that it found a match but had no
messageable agent, and continues without error. (`REQ-NOTIFY-5`)

**Scenario 5 — The watcher is stopped.**
The bootstrap manager closes the `svc-error-watcher` window (e.g. the service is
removed from `services.toml`, or the session is torn down). The watcher receives
SIGTERM and exits cleanly from its poll loop. (`REQ-SPAWN-2`)

**Scenario 6 — The watcher would match its own output.**
The watcher's previous alert ("Possible error/exception detected...") is visible
in its own `svc-error-watcher` window. Because the watcher excludes its own
window from scanning, this does not trigger a self-perpetuating alert loop.
(`REQ-SCAN-2`)

## Requirements

### Spawning & lifecycle (REQ-SPAWN)

- **REQ-SPAWN-1**: The watcher MUST be a new FCT lib (`libs/error_watcher/`,
  structured like `libs/app_watcher/`) exposing a console-script entry point
  (e.g. `error-watcher = "error_watcher.watcher:main"`), and MUST be registered
  as a `[services.error-watcher]` entry in `services.toml` so the bootstrap
  service manager launches it in its own `svc-error-watcher` tmux window.
- **REQ-SPAWN-2**: The watcher MUST run as a long-lived foreground loop that
  polls on a fixed interval consistent with the bootstrap manager's cadence
  (`POLL_INTERVAL = 5` seconds) and MUST install SIGTERM/SIGINT handlers that
  exit cleanly (mirroring `app_watcher`), so the bootstrap manager can stop it by
  closing its window.
- **REQ-SPAWN-3**: The watcher MUST discover its own session via
  `tmux display-message -p '#S'` (as the bootstrap manager's `_get_session_name`
  does) rather than assuming a hard-coded name. Single-instance-ness is owned by
  the bootstrap manager (one `svc-error-watcher` window); the watcher MUST NOT
  implement its own pidfile.
- **REQ-SPAWN-4**: A failure while scanning or messaging for any single window
  MUST NOT crash the loop; the watcher MUST log the failure and continue to the
  next poll. Restart-on-failure is provided by the `restart = "on-failure"`
  service policy.

### Window scanning (REQ-SCAN)

- **REQ-SCAN-1**: On each poll the watcher MUST enumerate every window of its
  session (`tmux list-windows -t <session> -F '#{window_name}'`) and capture each
  window's pane content (`tmux capture-pane -t <session>:<window> -p`).
- **REQ-SCAN-2**: The watcher MUST exclude its own window (`svc-error-watcher`)
  from scanning, to avoid a feedback loop where its own alert text (which
  contains the word "error") re-triggers a match.
- **REQ-SCAN-3**: The watcher MUST tolerate windows being created or destroyed
  between polls without erroring.
- **REQ-SCAN-4**: The watcher MUST scan the rendered pane text only; it MUST NOT
  depend on structured logs or transcript files.

### Match detection (REQ-MATCH)

- **REQ-MATCH-1**: The watcher MUST flag a window whose captured content contains
  text matching the regular expression `error|exception`.
- **REQ-MATCH-2**: Matching MUST be case-insensitive (`/error|exception/i`).
- **REQ-MATCH-3**: The watcher MUST alert only on newly-appeared matches. It MUST
  track which matches it has already alerted on (per window) and MUST NOT
  re-send for output it has already reported, so a static error on screen does
  not generate repeated alerts.
- **REQ-MATCH-4**: The match pattern SHOULD be defined in a single place so it
  can be adjusted without restructuring the script. It MAY be overridable via
  environment variable or config.

### Notification (REQ-NOTIFY)

- **REQ-NOTIFY-1**: When a poll surfaces one or more new matches, the watcher
  MUST enumerate agents via `mngr list --format json` (parsing the `agents`
  array, as `system_interface`'s `list_claude_agent_names` does) and send a
  message to one randomly selected messageable agent via
  `mngr message <name> -m <text>` (mirroring `telegram_bot`'s
  `_build_message_command` / subprocess pattern).
- **REQ-NOTIFY-2**: The message MUST identify where the error appeared (session
  name plus window name) and MUST include the matching line(s) for context.
- **REQ-NOTIFY-3**: "Messageable" MUST mean agents that can currently receive a
  message (per mngr's own rule, STOPPED agents are excluded). The watcher MUST
  NOT auto-start a stopped agent just to alert it. The agent that owns the
  offending window IS eligible (any agent is fair game).
- **REQ-NOTIFY-4**: If no messageable agent exists, the watcher MUST log the
  match and skip sending, without erroring (see Scenario 4).
- **REQ-NOTIFY-5**: Selection MUST be uniform random across the candidate pool.
- **REQ-NOTIFY-6**: When multiple windows produce new matches in the same poll,
  the watcher MUST send a single batched message to one randomly selected agent,
  not one message per window.

## Non-Goals

- Reducing false positives beyond the literal `/error|exception/i` match (e.g.
  ignoring "0 errors", "ErrorBoundary", or benign log lines). v1 is deliberately
  naive.
- Diagnosing, classifying, or fixing the detected error.
- Routing the alert to a *relevant* agent — selection is explicitly random, not
  smart.
- Persisting already-alerted state across watcher restarts (in-memory dedup is
  acceptable for v1; a restart may re-alert on errors still on screen).
- Watching anything other than the rendered tmux pane text (no log files, no
  transcript parsing).
- Any change to the monorepo `mngr_forever_claude` plugin — the watcher is
  entirely an FCT-side service.

## Resolved Decisions (formerly Open Questions)

- **Topology / session:** the watcher runs as a `services.toml` service inside
  the forever-claude agent's tmux session; it discovers the session name at
  runtime and scans the other windows. (Resolved.)
- **One instance:** single `svc-error-watcher` service. (Resolved.)
- **Candidate pool:** any messageable agent, including the source. (Resolved.)
- **mngr availability:** the watcher runs in the agent container where the
  vendored `mngr` CLI is on PATH (telegram_bot and system_interface already shell
  out to `mngr message` / `mngr list` from here). (Resolved.)
- **Language:** Python, mirroring `app_watcher`. (Resolved.)
- **Poll interval:** 5 seconds (the bootstrap manager's `POLL_INTERVAL`).
  (Resolved; MAY be made configurable later.)

## Open Questions

- **Scrollback vs visible pane.** v1 default: scan the visible pane only
  (`capture-pane -p`), accepting that errors that scroll past between polls may
  be missed. Including scrollback (`-S -`) is deferred; confirm this default is
  acceptable.
- **Message wording.** A default is proposed (see Scenario 1); confirm exact
  phrasing.
