# Task 1.1: Scaffold `libs/error_watcher/` and implement the pure core

## Goal

Create the new `error_watcher` library in the forever-claude-template (FCT) repo
and implement all of its pure, side-effect-free logic — error/exception
matching, per-window deduplication, alert message formatting, `mngr` argv
builders, agent-list parsing, and random recipient selection — with complete
unit tests. No tmux access or subprocess execution is wired up in this task
(that is Task 2.1); everything here is testable in isolation.

## Requirements addressed

REQ-MATCH-1, REQ-MATCH-2, REQ-MATCH-3, REQ-MATCH-4, REQ-NOTIFY-2, REQ-NOTIFY-5,
REQ-NOTIFY-6, and the package/entry-point half of REQ-SPAWN-1.

## Background

### What this feature is

The "Window Error Watcher" is a background service for the forever-claude-template
(FCT). An FCT agent runs inside a tmux session; a "bootstrap service manager"
(`uv run bootstrap`) reads a `services.toml` file and runs each declared service
in its own tmux window named `svc-<name>`, alongside window 0 (the Claude agent)
and injected `bootstrap` / `telegram` windows. The watcher is a new service that,
every few seconds, scans the other windows' on-screen text for the words "error"
or "exception" and, when new matching output appears, messages a random mngr
agent so a human-facing agent gets nudged.

### Where the code lives (critical)

- The implementation is built in the **FCT clone**, located at
  `.external_worktrees/forever-claude-template/` relative to the monorepo root,
  on git branch `preston/error-checker`. `.external_worktrees/` is gitignored by
  the monorepo, so the FCT clone is its own independent git repo — commit FCT
  changes there, on that branch.
- All file paths in this task are relative to the FCT clone root
  (`.external_worktrees/forever-claude-template/`).
- Before writing code, read the FCT root `style_guide.md` and
  `libs/app_watcher/`'s files (per the FCT CLAUDE.md "How to get started"
  section). Do NOT read `*_test.py` files except the ones this task explicitly
  references.

### The structural template to mirror: `libs/app_watcher/`

Mirror this package layout exactly:

- `libs/app_watcher/pyproject.toml` declares:
  - `[project] name = "app-watcher"`, `version = "0.1.0"`,
    `requires-python = ">=3.11"`, a `readme`, and dependencies.
  - `[project.scripts]` → `app-watcher = "app_watcher.watcher:main"`.
  - `[build-system] requires = ["hatchling"]`, `build-backend =
    "hatchling.build"`.
  - `[tool.hatch.build.targets.wheel] packages = ["src/app_watcher"]`.
- `libs/app_watcher/src/app_watcher/__init__.py` is **blank** (FCT CLAUDE.md: no
  code in `__init__.py`).
- `libs/app_watcher/src/app_watcher/watcher.py` holds the service code.
- `libs/app_watcher/test_app_watcher_ratchets.py` is the ratchet file.
- `libs/app_watcher/README.md` is a short description.

### Existing `mngr` CLI patterns to mirror (for the argv builders)

Two existing FCT modules build `mngr` argv as **pure functions** so they can be
validated against the live CLI contract:

- `libs/telegram_bot/src/telegram_bot/bot.py` →
  `_build_message_command(agent_name, message)` returns
  `["mngr", "message", agent_name, "-m", message]` (bot.py:59).
- `apps/system_interface/imbue/system_interface/claude_auth.py` →
  `_build_list_command()` returns `["mngr", "list", "--format", "json"]`
  (claude_auth.py:312), and `list_claude_agent_names()` (claude_auth.py:397)
  parses `json.loads(stdout)["agents"]` — a list of dicts each having `name` and
  `type` keys — and collects `name` where `type == "claude"`.

### The mngr CLI contract test helper

`libs/bootstrap/src/bootstrap/manager_test.py` imports
`from mngr_cli_contract.contract import assert_mngr_argv_valid` and uses it to
assert that a built `mngr` argv is accepted by the live vendored mngr CLI
(`vendor/mngr`). `mngr_cli_contract` is a workspace package available in the test
environment (bootstrap does not list it as a runtime dependency). Use the same
import and helper in this task's tests for the argv builders.

## Files to modify/create

(All under the FCT clone, branch `preston/error-checker`.)

- `libs/error_watcher/pyproject.toml` — new; copy `libs/app_watcher/pyproject.toml`
  and adapt: `name = "error-watcher"`, description, `[project.scripts]`
  `error-watcher = "error_watcher.watcher:main"`, wheel package
  `["src/error_watcher"]`. Dependencies: `loguru` (logging; matches
  `telegram_bot`/`bootstrap`). Do NOT copy app_watcher's `inotify_simple` /
  `httpx` / `imbue-common` deps unless you actually use them (you don't here).
- `libs/error_watcher/src/error_watcher/__init__.py` — new; **blank**.
- `libs/error_watcher/src/error_watcher/watcher.py` — new; the pure functions
  below. `main()` may be a stub that does nothing yet OR raise a clear "not yet
  wired" message — but note Task 2.1 replaces it; do NOT register the service
  until Task 3.1, so a stub is harmless. Prefer leaving `main()` as a thin stub
  that Task 2.1 fills in. (Do not add a TODO comment — FCT ratchet forbids it;
  just leave a one-line docstring saying the loop is implemented in Task 2.1's
  scope.)
- `libs/error_watcher/src/error_watcher/watcher_test.py` — new; unit tests for
  every pure function.
- `libs/error_watcher/test_error_watcher_ratchets.py` — new; copy
  `libs/app_watcher/test_app_watcher_ratchets.py` and regenerate the snapshot
  counts for this package (see Gotchas).
- `libs/error_watcher/README.md` — new; a short description (fuller docs land in
  Task 3.1, a stub sentence is fine here).

## Implementation details

Implement these pure functions in `watcher.py`. Keep them free of `subprocess`,
`tmux`, `time.sleep`, and file I/O — those belong to Task 2.1.

1. **Pattern (REQ-MATCH-1, -2, -4):** define a single module-level compiled
   regex, e.g. `ERROR_PATTERN = re.compile(r"error|exception", re.IGNORECASE)`.
   This is the single source of truth for the match (REQ-MATCH-4). Optionally
   read an override from an env var name (e.g. `ERROR_WATCHER_PATTERN`) at
   `main()` startup in Task 2.1 — but the default constant lives here.

2. **`match_lines(text: str) -> list[str]`** — split `text` into lines and return
   the lines that contain a match for `ERROR_PATTERN`. Case-insensitive
   (REQ-MATCH-2). Return them in order, de-duplicating identical lines within a
   single capture is fine but not required (the per-window tracker handles
   cross-poll dedup).

3. **Per-window dedup (REQ-MATCH-3).** Implement a small helper that, given a
   window name, the set of matching lines currently on screen, and a mutable
   "already alerted" store (e.g. `dict[str, set[str]]` keyed by window name),
   returns only the matching lines that have NOT been alerted before for that
   window, and records them as seen. Signature suggestion:
   `new_matches(window: str, current: list[str], seen: dict[str, set[str]]) ->
   list[str]`. A line already in `seen[window]` is suppressed; new lines are
   returned and added to `seen[window]`. This makes a static error on screen
   alert exactly once (Scenario 2).

4. **`format_alert(session: str, matches_by_window: dict[str, list[str]]) ->
   str`** (REQ-NOTIFY-2, REQ-NOTIFY-6). Given the new matches grouped by window,
   build a single human-readable message naming each window and including its
   matching line(s), e.g.:
   `"Possible error/exception detected by error-watcher in session '<session>':\n
   - window 'svc-web': <line>\n - window 'svc-api': <line1> | <line2>"`.
   One message covers all windows that newly matched this poll (batching —
   REQ-NOTIFY-6). Keep the matching lines verbatim but consider truncating each
   to a sane length (e.g. 500 chars) so a giant traceback line doesn't blow up
   the message; if you truncate, append an ellipsis.

5. **`build_list_command() -> list[str]`** → `["mngr", "list", "--format",
   "json"]` (mirror `claude_auth._build_list_command`).

6. **`build_message_command(agent_name: str, message: str) -> list[str]`** →
   `["mngr", "message", agent_name, "-m", message]` (mirror
   `telegram_bot._build_message_command`).

7. **`parse_agent_names(stdout: str) -> list[str]`** — parse `mngr list --format
   json` output: `json.loads(stdout)`, expect a dict with an `"agents"` list of
   dicts, collect each `name` (str, non-empty). Mirror the defensive checks in
   `claude_auth.list_claude_agent_names` (raise a clear error / or return `[]` on
   malformed input — choose return `[]` plus a `loguru` warning here, since the
   watcher must never crash its loop per REQ-SPAWN-4). NOTE: status/messageable
   filtering is added in Task 2.1 once the exact status field is confirmed; for
   this task, parse and return all agent names (and, if a status field is
   present in the JSON, expose it so Task 2.1 can filter — e.g. return a list of
   small dataclasses/dicts with `name` and `status`). Read the actual
   `mngr list --format json` shape (run it, or read `vendor/mngr`) to decide the
   return type; document the chosen shape in the function docstring.

8. **`choose_recipient(names: Sequence[str], rng: random.Random) -> str | None`**
   (REQ-NOTIFY-5) — return a uniformly random element, or `None` if empty. Take
   the `random.Random` instance as a parameter so tests are deterministic
   (seeded) — do NOT call the module-level `random` functions directly inside the
   function.

### Unit tests (`watcher_test.py`)

Cover, at minimum:

- `match_lines`: matches "Error", "ERROR", "exception", "Traceback ...
  Exception" case-insensitively (REQ-MATCH-2); returns `[]` for clean output;
  matches substrings like "0 errors" / "ErrorBoundary" (documents the
  deliberately-naive behavior — these ARE expected to match per the spec's
  Non-Goals).
- `new_matches`: first call returns the new lines and records them; a second call
  with the same lines returns `[]` (REQ-MATCH-3); a later call with an
  additional new line returns only the new one.
- `format_alert`: includes the session name, each window name, and each matching
  line; one message covers multiple windows (REQ-NOTIFY-6); truncation behaves if
  you implemented it.
- `build_list_command` / `build_message_command`: assert exact argv, AND validate
  with `from mngr_cli_contract.contract import assert_mngr_argv_valid` (mirror
  `manager_test.py` / `bot_test.py`).
- `parse_agent_names`: parses a representative `mngr list --format json` payload;
  returns `[]` (not raises) on malformed/non-JSON input.
- `choose_recipient`: with a seeded `random.Random`, returns a deterministic
  expected element; returns `None` for an empty sequence.

## Testing suggestions

- Run just this package: `cd libs/error_watcher && uv run pytest`. For fast
  iteration add `-m 'not tmux and not modal and not docker and not docker_sdk
  and not acceptance and not release' --no-cov --cov-fail-under=0`.
- The end-to-end / integration test that exercises a full poll cycle is added in
  Task 2.1 (`watcher_test.py::test_run_one_poll_*`). This task's tests are pure
  unit tests; there are no tmux/subprocess tests here by design.

## Gotchas

- `__init__.py` MUST be blank (FCT CLAUDE.md). Put no code there.
- No `__all__`, no `TYPE_CHECKING` import guard (FCT CLAUDE.md).
- No `TODO`/`FIXME` comments — the ratchet `test_prevent_todos` is `snapshot(0)`.
- **Ratchet snapshots:** `test_error_watcher_ratchets.py` will fire on real
  counts. Generate them by running, from the FCT root,
  `uv run pytest --inline-snapshot=create libs/error_watcher/test_error_watcher_ratchets.py`
  (run WITHOUT xdist so inline-snapshot is active). If you use `loguru` (not bare
  `print`), `test_prevent_bare_print` should land at `snapshot(0)` rather than
  app_watcher's `snapshot(5)`. Do not blindly copy app_watcher's numbers.
- `choose_recipient` taking an injected `random.Random` is what makes
  REQ-NOTIFY-5 testable — resist calling `random.choice` directly.
- Keep `parse_agent_names` tolerant: the loop must never crash (REQ-SPAWN-4), so
  malformed JSON → warn + return empty, not raise.

## Verification checklist

- [ ] `libs/error_watcher/` exists with `pyproject.toml`, blank
  `src/error_watcher/__init__.py`, `src/error_watcher/watcher.py`,
  `src/error_watcher/watcher_test.py`, `test_error_watcher_ratchets.py`,
  `README.md`.
- [ ] `pyproject.toml` declares the `error-watcher = "error_watcher.watcher:main"`
  console script and `packages = ["src/error_watcher"]`.
- [ ] All pure functions implemented: `match_lines`, `new_matches`,
  `format_alert`, `build_list_command`, `build_message_command`,
  `parse_agent_names`, `choose_recipient`.
- [ ] `build_list_command` / `build_message_command` are validated with
  `assert_mngr_argv_valid` in the tests.
- [ ] `cd libs/error_watcher && uv run pytest` passes (unit + ratchet tests).
- [ ] Ratchet snapshot counts were generated for THIS package, not copied from
  app_watcher.
- [ ] Unit tests: `libs/error_watcher/src/error_watcher/watcher_test.py` covers
  match detection (incl. case-insensitivity and the naive "0 errors" match),
  dedup, alert formatting/batching, both argv builders, agent parsing, and random
  choice.

## Commit policy

Commit this task's work in the FCT clone on branch `preston/error-checker` with a
descriptive message. End the message body with:

```
Co-authored-by: Sculptor <sculptor@imbue.com>
```
