Strengthened the test suite for the `mngr` CLI (the `imbue/mngr/cli` package), addressing fragile, misleading, or non-falsifiable tests surfaced by the `identify-bad-tests` review. No user-visible CLI behavior changed; these are test-quality improvements plus two small internal seams to make the tests deterministic.

Highlights:

- Tests that previously asserted nothing (only "no exception raised") now assert observable output: dependency status tables, plugin list rendering, extras status lines, message/event human-format output, and JSONL summaries.
- Tests that passed for the wrong reason were tightened: removed always-true `or` disjunctions and substring checks that matched unrelated output (version flag, snapshot/git help, stopped/remote list filters, doc-link ref shape, alias completion), and pinned exact strings, exit codes, and error types/messages on error paths (destroy/rename/connect/exec/migrate/rsync/snapshot/ask not-found and usage errors).
- Replaced tautological "construct-and-echo" `*CliOptions` round-trip tests with `cli_runner`-driven tests that assert the parsed option actually reflects the flag, or deleted them where no meaningful mapping existed.
- Removed real-network/real-subprocess calls from unit tests (GitHub issue search, the claude-plugin install path, install-wizard signal checks), making them deterministic.
- Replaced collision-prone `int(time.time())` agent/session names with `uuid4().hex`.
- Added missing branch coverage: rsync/git endpoint-resolution helpers, the gc `machine_record_count` JSONL path, limit `changes` payloads and invalid-activity-config rejection, and the install-wizard phase-2 signal gating and package de-duplication.
- Moved two `api/find` tests out of the `cli/agent_utils` test module to colocate with the code they exercise.

Two small internal changes support the above: the install wizard's phase-2 selection/gating/dedup logic was extracted into pure, unit-testable helpers and `_should_preselect_basic` gained an injectable signal-check function; and a `get_tmux_pane_pids` test helper was added so the `start --restart` tests can assert the agent process was actually replaced.
