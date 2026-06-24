# Task 2.1: Implement the poll loop, tmux capture, and mngr messaging

## Goal

Wire the long-running service: a `main()` poll loop that discovers its tmux
session, enumerates and captures the other windows, runs the pure detection/dedup
core from Task 1.1, and on newly-appeared matches enumerates messageable mngr
agents and sends a single batched alert to one at random. Add SIGTERM/SIGINT
handlers for clean shutdown, a 5-second cadence, and per-window error isolation
so one failure never kills the loop. Add a non-flaky integration test that drives
one full poll iteration with injected fakes (no real tmux/subprocess).

## Requirements addressed

REQ-SPAWN-2, REQ-SPAWN-3, REQ-SPAWN-4, REQ-SCAN-1, REQ-SCAN-2, REQ-SCAN-3,
REQ-SCAN-4, REQ-NOTIFY-1, REQ-NOTIFY-3, REQ-NOTIFY-4.

## Background

### What this feature is

The Window Error Watcher is a forever-claude-template (FCT) background service.
An FCT agent runs in a tmux session; a bootstrap service manager (`uv run
bootstrap`) runs each `services.toml` service in its own tmux window named
`svc-<name>`. The watcher polls the session's other windows for on-screen text
matching `/error|exception/i` and, on new matches, messages a random mngr agent.

### Where the code lives (critical)

- Built in the **FCT clone** at `.external_worktrees/forever-claude-template/`
  (relative to the monorepo root), git branch `preston/error-checker`. That
  directory is gitignored by the monorepo and is its own git repo — commit there.
- All paths below are relative to the FCT clone root.

### What Task 1.1 produced (this task builds on it)

Task 1.1 created `libs/error_watcher/` with `pyproject.toml` (console script
`error-watcher = "error_watcher.watcher:main"`), a blank `__init__.py`, and these
pure functions in `src/error_watcher/watcher.py` (all unit-tested in
`watcher_test.py`):

- `ERROR_PATTERN` (compiled `re` with `IGNORECASE`) and
  `match_lines(text) -> list[str]`.
- `new_matches(window, current, seen) -> list[str]` — per-window dedup against a
  mutable `seen: dict[str, set[str]]`.
- `format_alert(session, matches_by_window) -> str` — one batched message naming
  each window and its matching lines.
- `build_list_command() -> ["mngr", "list", "--format", "json"]`.
- `build_message_command(name, message) -> ["mngr", "message", name, "-m",
  message]`.
- `parse_agent_names(stdout) -> ...` — parses `mngr list --format json`'s
  `agents` array (return shape chosen in Task 1.1; may include a status field).
- `choose_recipient(names, rng) -> str | None` — uniform random, takes an
  injected `random.Random`.

This task fills in `main()` (Task 1.1 left it a stub) and adds the I/O seam
functions plus their integration test.

### The bootstrap / tmux model to follow

Read `libs/bootstrap/src/bootstrap/manager.py` for the exact tmux invocations:

- `_get_session_name()` (manager.py:423) →
  `subprocess.run(["tmux", "display-message", "-p", "#S"], ...)` returns the
  current session name. Use the same approach (REQ-SPAWN-3).
- `_list_managed_windows()` (manager.py:433) →
  `["tmux", "list-windows", "-t", session, "-F", "#{window_name}"]`. Use the same
  to enumerate ALL window names (do not filter to `svc-` — the spec wants every
  window, e.g. window 0/the agent, `bootstrap`, `telegram`, and all `svc-*`)
  (REQ-SCAN-1).
- Capture a window's visible pane:
  `["tmux", "capture-pane", "-t", f"{session}:{window_name}", "-p"]`
  (REQ-SCAN-1, REQ-SCAN-4). Visible pane only for v1 (no `-S -`).
- `SVC_PREFIX = "svc-"`, `POLL_INTERVAL = 5` (manager.py:45, :59).

### The signal/loop model to follow: `libs/app_watcher/src/app_watcher/watcher.py`

- `main()` runs `while True:` and at the bottom sleeps for the poll interval.
- It installs handlers: `signal.signal(signal.SIGTERM, _handle_signal)` and
  `signal.signal(signal.SIGINT, _handle_signal)`, where `_handle_signal` calls
  `sys.exit(0)` (watcher.py:187-191). Mirror this — it's how the bootstrap
  manager stops the service (Scenario 5, REQ-SPAWN-2).

### The mngr messaging model to follow

- Send: `libs/telegram_bot/src/telegram_bot/bot.py` `_send_to_agent`
  (bot.py:66) runs the argv via `subprocess.run(..., check=True,
  capture_output=True, text=True)` and, on `subprocess.CalledProcessError`, logs
  `e.stderr` with `loguru` and does NOT re-raise. Mirror this for sending
  (REQ-NOTIFY-1).
- Enumerate: `apps/system_interface/imbue/system_interface/claude_auth.py`
  `list_claude_agent_names` (claude_auth.py:397) runs `mngr list --format json`
  and parses `agents`. Use Task 1.1's `parse_agent_names` on the captured stdout.
- Self name: env var `MNGR_AGENT_NAME` (telegram_bot.bot:83) — not needed for
  selection since any agent (including self) is eligible, but available if you
  log "alerted <name>".

## Files to modify/create

(All under the FCT clone, branch `preston/error-checker`.)

- `libs/error_watcher/src/error_watcher/watcher.py` — modify: add the I/O seam
  functions and implement `main()`.
- `libs/error_watcher/src/error_watcher/watcher_test.py` — modify: add the
  integration test(s) for `run_one_poll` with injected fakes.

## Implementation details

Factor the I/O so a full poll iteration is testable without real tmux/subprocess.
Use the injected-callable pattern from `claude_auth.py` (its `command_runner`
seam).

1. **Command runner seam.** Define a small callable type alias for "run argv,
   return (returncode, stdout, stderr)" and a default implementation that calls
   `subprocess.run(argv, capture_output=True, text=True, timeout=...)`. `main()`
   uses the default; tests inject a fake.

2. **Window helpers** (thin wrappers over the command runner):
   - `get_session_name(run) -> str` — `tmux display-message -p '#S'`.
   - `list_windows(run, session) -> list[str]` — `tmux list-windows ...
     '#{window_name}'`, split lines. On non-zero/empty, return `[]` and warn.
   - `capture_window(run, session, window) -> str` — `tmux capture-pane -t
     session:window -p`. On failure return `""` and warn (REQ-SCAN-3: a window
     that vanished mid-poll must not crash the loop).

3. **`OWN_WINDOW = "svc-error-watcher"`** constant. In the scan, skip the window
   whose name equals `OWN_WINDOW` (REQ-SCAN-2) — this prevents the watcher's own
   alert text (which contains "error") from re-triggering (Scenario 6).

4. **`run_one_poll(run, seen, rng) -> str | None`** — the testable heart of the
   loop. Steps:
   a. `session = get_session_name(run)`.
   b. For each window in `list_windows(run, session)` except `OWN_WINDOW`:
      capture its pane, compute `match_lines`, then `new_matches(window, lines,
      seen)`; collect non-empty results into `matches_by_window`.
   c. If `matches_by_window` is empty, return `None` (nothing new).
   d. Else build `message = format_alert(session, matches_by_window)`.
   e. Enumerate agents: run `build_list_command()`, `parse_agent_names(stdout)`,
      then filter to messageable agents (REQ-NOTIFY-3) — see step 6 below.
   f. `recipient = choose_recipient(messageable_names, rng)`. If `None`, log the
      match and return `None` without sending (REQ-NOTIFY-4, Scenario 4).
   g. Run `build_message_command(recipient, message)` via the runner; on
      non-zero exit, warn (don't raise). Return `recipient` (useful for the test
      and logging).
   Wrap the per-window body (b) so one window's failure is caught, logged, and
   skipped (REQ-SPAWN-4, REQ-SCAN-3).

5. **`main()`**: build the default runner, a `seen: dict[str, set[str]] = {}`, and
   a `rng = random.Random()` (unseeded — real randomness). Optionally read
   `ERROR_WATCHER_PATTERN` env override and recompile `ERROR_PATTERN` if set.
   Install SIGTERM/SIGINT handlers (mirror app_watcher). Then loop:
   `while True: run_one_poll(...) wrapped in try/except (log + continue);
   time.sleep(POLL_INTERVAL)`. Log startup with `loguru` (mirror telegram_bot).

6. **Messageable filtering (REQ-NOTIFY-3).** Determine the agent status/state
   field by running `mngr list --format json` in the FCT clone (or reading the
   `vendor/mngr` list command source) and inspecting the JSON. Filter out agents
   that cannot receive a message (STOPPED). If — and only if — there is no usable
   status field in the JSON, fall back to: attempt the send and, on a non-zero
   exit that indicates the agent is stopped, log and skip (do not retry forever).
   Document whichever approach you took in the code and in this task's PR notes.
   STOPPED agents MUST NOT be auto-started.

### Integration test (`watcher_test.py`)

Add `test_run_one_poll_sends_alert_for_new_error` and friends, driving
`run_one_poll` with a **fake command runner** (a callable that returns canned
`(returncode, stdout, stderr)` keyed on the argv it receives):

- Fake `tmux display-message` → a session name.
- Fake `tmux list-windows` → e.g. `svc-web`, `svc-api`, `svc-error-watcher`,
  `bootstrap`.
- Fake `tmux capture-pane` per window: `svc-web` returns text containing
  `Traceback ... Exception`, others clean; `svc-error-watcher` returns text
  containing the word "error" (to prove it's skipped — REQ-SCAN-2).
- Fake `mngr list --format json` → a JSON payload with two messageable agents.
- Capture the argv passed to `mngr message` and assert: exactly one message
  send happened (batching — REQ-NOTIFY-6), the recipient is one of the two
  agents, and the message body contains `svc-web` and the matching line but NOT
  `svc-error-watcher`.
- Seed `rng = random.Random(0)` so the chosen recipient is deterministic.
- Add cases: second identical poll sends nothing (dedup — REQ-MATCH-3); no
  messageable agents → no send, returns `None` (REQ-NOTIFY-4); a window whose
  capture "fails" (fake returns non-zero) doesn't crash the poll (REQ-SCAN-3).

This is an integration test of the loop body with fakes — it is NOT a real-tmux
test and is safe for CI.

### Manual verification (NOT crystallized into pytest)

Per the FCT CLAUDE.md ("Verifying interactive components with tmux"), do a live
check by hand and do NOT turn it into a pytest test (it would be flaky):

1. In the FCT clone, start a tmux session with a couple of windows; in one,
   `echo "Traceback ... Exception: boom"`.
2. Run the watcher in another window: `cd libs/error_watcher && uv run
   error-watcher` (or `MNGR_AGENT_NAME=... uv run error-watcher`).
3. Confirm it logs a detected match for the right window and attempts a
   `mngr message` (or logs "no messageable agent" if none).
4. Confirm a second poll with the same screen does NOT re-alert.
5. Confirm it ignores its own `svc-error-watcher` window.

## Testing suggestions

- `cd libs/error_watcher && uv run pytest` (fast: add `-m 'not tmux and not
  modal and not docker and not docker_sdk and not acceptance and not release'
  --no-cov --cov-fail-under=0`).
- End-to-end coverage for this task = the `run_one_poll` integration test
  (`libs/error_watcher/src/error_watcher/watcher_test.py::test_run_one_poll_*`)
  plus the manual tmux check above. There is intentionally no real-tmux pytest.

## Gotchas

- **Self-window feedback loop:** forgetting to skip `svc-error-watcher`
  (REQ-SCAN-2) makes the watcher alert on its own alert text every poll. The
  integration test asserts this is skipped.
- **Never crash the loop (REQ-SPAWN-4):** every subprocess/tmux call must be
  guarded; a failure logs and continues. The loop is the service's whole job.
- `test_prevent_while_true` and `test_prevent_time_sleep` ratchets: the `main()`
  loop legitimately needs `while True` and `time.sleep`. Regenerate the package's
  ratchet snapshots (`uv run pytest --inline-snapshot=create
  libs/error_watcher/test_error_watcher_ratchets.py`, no xdist) so the counts
  reflect the real, justified usage (app_watcher uses `snapshot(1)` for each).
- Use an injected `random.Random` in `run_one_poll` (passed down to
  `choose_recipient`) so the test is deterministic; only `main()` constructs the
  unseeded real RNG.
- Give subprocess calls a `timeout` so a hung `mngr`/`tmux` can't wedge the loop.
- `capture-pane -p` returns the visible pane only; that's the v1 decision. Do not
  add `-S -` (scrollback) — it's an explicit Open Question, out of scope here.

## Verification checklist

- [ ] `main()` discovers the session via `tmux display-message -p '#S'`, loops at
  `POLL_INTERVAL = 5`, and installs SIGTERM/SIGINT handlers that exit cleanly.
- [ ] `run_one_poll` enumerates all windows, captures panes, skips
  `svc-error-watcher`, applies dedup, and sends exactly one batched message to a
  random messageable agent when there are new matches.
- [ ] STOPPED agents are excluded; no messageable agent → logs and skips, no
  crash.
- [ ] Every tmux/mngr call is guarded so the loop survives individual failures.
- [ ] Integration test passes:
  `libs/error_watcher/src/error_watcher/watcher_test.py::test_run_one_poll_*`
  (new-error alert, dedup-on-repeat, no-agents skip, capture-failure tolerated,
  own-window skipped).
- [ ] `cd libs/error_watcher && uv run pytest` passes.
- [ ] Manual tmux verification performed (not crystallized into pytest).
- [ ] End-to-end tests: the `run_one_poll` integration tests above (real-tmux
  E2E is intentionally manual per FCT convention).

## Commit policy

Commit this task's work in the FCT clone on branch `preston/error-checker` with a
descriptive message ending in:

```
Co-authored-by: Sculptor <sculptor@imbue.com>
```
