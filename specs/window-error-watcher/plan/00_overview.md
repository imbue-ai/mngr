# Window Error Watcher — Implementation Plan

## Summary

Add a new forever-claude-template (FCT) service, `error-watcher`, that polls
every tmux window in its session for output matching `/error|exception/i` and,
on newly-appeared matches, sends one message to a randomly chosen mngr agent.
It is a new lib (`libs/error_watcher/`, structured like `libs/app_watcher/`)
registered as a `[services.error-watcher]` entry so the bootstrap service
manager spawns it like any other service.

**Important:** the implementation is built in the FCT clone at
`.external_worktrees/forever-claude-template/` (branch `preston/error-checker`),
not the monorepo. The spec and this plan live in the monorepo at
`specs/window-error-watcher/`. See `_exploration_notes.md` for the full
codebase map. No monorepo code changes are needed.

## Phases

- **Phase 1: Pure core + package skeleton** — Create the `libs/error_watcher/`
  package and implement all pure, side-effect-free logic (regex matching, per-
  window dedup, alert formatting, `mngr` argv builders, agent-list parsing,
  random recipient choice) with full unit tests.
- **Phase 2: Poll loop and I/O** — Wire the long-running `main()` loop: session
  discovery, window enumeration + pane capture (excluding the watcher's own
  window), agent enumeration + random messaging via `mngr`, signal handling, and
  the 5s cadence. Include a non-flaky integration test of one poll iteration with
  injected fakes, plus manual tmux verification.
- **Phase 3: Service registration + docs** — Register `[services.error-watcher]`
  in `services.toml`, add the lib README and changelog entry, and verify the
  bootstrap manager spawns it end-to-end.
- **Phase 99: Finalize** — Run all tests added by the plan; launch the Review
  agent.

## Phase Rationale

The pure core (Phase 1) carries all the testable behavior and has zero
dependencies on tmux or subprocesses, so it can be fully unit-tested in
isolation before any I/O exists. Phase 2 builds the thin I/O shell around that
verified core, factored so a single poll iteration is testable with injected
fakes (the `command_runner` injection pattern from `claude_auth.py`) rather than
real tmux. Phase 3 only flips the service on once the code behind it works,
avoiding a crash-looping window during development. This is a thin-vertical-slice
ordering: after Phase 2 the watcher is fully functional when run by hand; Phase 3
just makes bootstrap own its lifecycle.

## Test Strategy note

Per the FCT CLAUDE.md ("Verifying interactive components with tmux"), live tmux
interaction is **not** crystallized into pytest — such tests are flaky and
useless in CI. End-to-end confidence therefore comes from three layers:
(1) unit tests on the pure core (Phase 1), (2) an integration test that drives
one full `run_one_poll` iteration with injected fakes — no real tmux/subprocess
(Phase 2), and (3) manual `tmux send-keys` / `tmux capture-pane` verification
(Phases 2 and 3). This is a deliberate, repo-sanctioned deviation from forcing a
flaky tmux pytest; it is called out in the affected task files.

## Task Index

| File | Task | Phase | Requirements |
|------|------|-------|-------------|
| `01_01_scaffold_lib_and_pure_core.md` | Create `libs/error_watcher/` package and implement + unit-test all pure logic | 1 | REQ-MATCH-1, REQ-MATCH-2, REQ-MATCH-3, REQ-MATCH-4, REQ-NOTIFY-2, REQ-NOTIFY-5, REQ-NOTIFY-6, REQ-SPAWN-1 (partial) |
| `02_01_poll_loop_and_io.md` | Implement the `main()` poll loop, tmux capture, mngr messaging, signals; integration test | 2 | REQ-SPAWN-2, REQ-SPAWN-3, REQ-SPAWN-4, REQ-SCAN-1, REQ-SCAN-2, REQ-SCAN-3, REQ-SCAN-4, REQ-NOTIFY-1, REQ-NOTIFY-3, REQ-NOTIFY-4 |
| `03_01_register_service_and_docs.md` | Register the service in `services.toml`, add README + changelog, verify end-to-end | 3 | REQ-SPAWN-1, REQ-SPAWN-4 |
| `99_01_verify_all_tests.md` | Run all tests added in this plan and iterate to green | 99 | (all) |
| `99_02_launch_review.md` | Launch the Review agent | 99 | (all) |

## Requirements coverage

Every spec requirement is covered:

- REQ-SPAWN-1: 01_01 (entry point + package) + 03_01 (service registration)
- REQ-SPAWN-2, -3, -4: 02_01 (loop, session discovery, per-window error
  isolation) + 03_01 (restart policy)
- REQ-SCAN-1..4: 02_01
- REQ-MATCH-1..4: 01_01
- REQ-NOTIFY-1, -3, -4: 02_01
- REQ-NOTIFY-2, -5, -6: 01_01 (alert formatting, random choice, batching)
