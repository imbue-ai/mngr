# Exploration Notes — Window Error Watcher

## Where this is built

- **Implementation lives in the forever-claude-template (FCT) repo**, cloned at
  `.external_worktrees/forever-claude-template/` on branch `preston/error-checker`
  (same branch name as this monorepo, per the monorepo CLAUDE.md external-worktree
  rule). `.external_worktrees/` is gitignored by the monorepo.
- The **spec and this plan** live in the monorepo at
  `specs/window-error-watcher/`. The build agent commits implementation to the
  FCT clone's branch and the spec/plan to the monorepo branch — two separate
  commits in two separate repos.
- **No monorepo code changes** are required. The monorepo `mngr_forever_claude`
  plugin only injects the `bootstrap`/`telegram` windows; the watcher is a
  `services.toml`-managed FCT service.

All paths below are relative to the FCT clone root
(`.external_worktrees/forever-claude-template/`) unless noted.

## The bootstrap / services / windows model

- `libs/bootstrap/src/bootstrap/manager.py` is the service manager (`uv run
  bootstrap`). Key facts:
  - `_get_session_name()` (manager.py:423) → `tmux display-message -p '#S'`.
    This is how any service discovers its own session name at runtime.
  - `SVC_PREFIX = "svc-"` (manager.py:45). Each `[services.<name>]` runs in a
    tmux window named `svc-<name>`.
  - `POLL_INTERVAL = 5` seconds (manager.py:59).
  - `_list_managed_windows()` (manager.py:433) uses
    `tmux list-windows -t <session> -F '#{window_name}'`.
  - `_start_service()` (manager.py:576) creates the window with `tmux new-window`
    and the service inherits `MNGR_AGENT_STATE_DIR` etc. from the session's
    default-command env.
  - `restart = "on-failure"` re-launches a service that exits non-zero (README
    "Restart policy").
- `services.toml` (FCT root) — current entries: `system_interface`, `web`,
  `cloudflared`, `app-watcher`, `runtime-backup`, `host-backup`,
  `deferred-install`. Each is `command = "..."` + optional `restart`.
- The tmux session also contains window 0 (the Claude agent) and the injected
  `bootstrap` and `telegram` windows (see monorepo
  `specs/forever-claude-plugin/concise.md`).

## The structural template: `libs/app_watcher/`

The watcher should mirror `app_watcher` almost exactly in structure:

- `libs/app_watcher/pyproject.toml`:
  - `name = "app-watcher"`, `requires-python = ">=3.11"`
  - `[project.scripts]` → `app-watcher = "app_watcher.watcher:main"`
  - `[tool.hatch.build.targets.wheel] packages = ["src/app_watcher"]`
  - deps include `imbue-common` (workspace pkg) and `inotify_simple`.
- `libs/app_watcher/src/app_watcher/watcher.py` (226 lines) — the service body:
  - `main()` with a `while True:` poll loop.
  - `signal.signal(SIGTERM/SIGINT, handler)` where the handler calls
    `sys.exit(0)` (watcher.py:187-191). This is how bootstrap stops it.
  - Uses `print(..., file=sys.stderr, flush=True)` for logging (bare prints —
    the ratchet allows 5; see below). `telegram_bot`/`bootstrap` use `loguru`
    instead; either is acceptable, prefer `loguru` for new code per FCT style.
  - `__init__.py` is blank.
- `libs/app_watcher/test_app_watcher_ratchets.py` — ratchet file using
  `from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc`
  and `inline_snapshot.snapshot(...)`. Notable allowances we will also need:
  `test_prevent_while_true → snapshot(1)`, `test_prevent_time_sleep →
  snapshot(1)`. The build agent regenerates snapshots with
  `uv run pytest --inline-snapshot=create` (run without xdist).
- `app_watcher` has **no** `watcher_test.py` (only the ratchet file). We will add
  one for our pure logic — that's an improvement, not a deviation.

## Messaging mngr agents

Two existing references — both build the `mngr` argv as a **pure function** so
they can be validated against the live CLI contract:

- **Send:** `libs/telegram_bot/src/telegram_bot/bot.py`
  - `_build_message_command(agent_name, message)` (bot.py:59) →
    `["mngr", "message", agent_name, "-m", message]`.
  - `_send_to_agent()` (bot.py:66) runs it via `subprocess.run(..., check=True,
    capture_output=True, text=True)` and logs `e.stderr` on
    `CalledProcessError`. Mirror this exactly.
- **Enumerate:** `apps/system_interface/imbue/system_interface/claude_auth.py`
  - `_build_list_command()` (claude_auth.py:312) → `["mngr", "list", "--format",
    "json"]`.
  - `list_claude_agent_names()` (claude_auth.py:397) parses
    `json.loads(stdout)["agents"]` — a list of dicts with `name` and `type`
    fields, filters `type == "claude"`, collects `name`.
- **Self name:** env var `MNGR_AGENT_NAME` (used by telegram_bot.bot:83).
- **Messageable filtering:** mngr itself refuses to message a STOPPED agent (see
  monorepo `libs/mngr/imbue/mngr/api/message.py` `_send_message_to_agent`). The
  exact agent-status field in `mngr list --format json` output is **not yet
  confirmed** — the build agent MUST run `mngr list --format json` (or read
  `vendor/mngr` list command) to find the status/state key, then filter
  messageable agents on it. Spec REQ-NOTIFY-3/4 require this. Simplest robust
  fallback if no clear status field exists: attempt `mngr message` and treat a
  non-zero exit as "not messageable, try another / skip" — but prefer explicit
  filtering.

## Testing conventions (FCT)

- Unit tests are co-located `*_test.py` (e.g. `libs/bootstrap/src/bootstrap/
  manager_test.py`, `libs/telegram_bot/bot_test.py`). Pure functions are tested
  directly; subprocess/tmux are NOT spawned in unit tests.
- `mngr_cli_contract.contract.assert_mngr_argv_valid` (imported in
  `manager_test.py`) validates that a built `mngr` argv is accepted by the live
  vendored mngr CLI. Use it on our `_build_list_command()` /
  `_build_message_command()`.
- Run tests per-project: `cd libs/error_watcher && uv run pytest`. Fast iteration:
  add `-m 'not tmux and not modal and not docker and not docker_sdk and not
  acceptance and not release' --no-cov --cov-fail-under=0`. Final check: full
  suite, no `--no-cov`.
- **tmux interaction is NOT crystallized into pytest** (FCT CLAUDE.md "Verifying
  interactive components with tmux"): such tests are flaky and useless in CI.
  We instead (a) unit-test the pure core, (b) integration-test a single poll
  iteration with injected fakes (no real tmux/subprocess), and (c) verify the
  live behavior manually with `tmux send-keys` / `tmux capture-pane`.

## Design seams for testability

Factor `watcher.py` so the loop body is testable without real tmux/subprocess:

- Pure: `match_lines(text) -> list[str]`, `new_matches(window, matches, seen)`,
  `format_alert(session, matches_by_window) -> str`, `build_list_command()`,
  `build_message_command(name, msg)`, `parse_agent_names(stdout) -> list[str]`,
  `choose_recipient(names, rng) -> str | None`.
- A `run_one_poll(...)` that takes injected callables for "list windows",
  "capture window", and "run command" (the command runner pattern from
  `claude_auth.py`'s `command_runner`), so a test can drive a full poll with
  fakes and assert that the right `mngr message` argv is produced for a seeded
  error — non-flaky, no tmux.

## Conventions / housekeeping

- **Changelog:** FCT uses a single top-level `changelog/<branch>.md` (e.g.
  `changelog/aws-minds-compute-provider.md`). Add
  `changelog/preston-error-checker.md` describing the new service.
- **Style:** read FCT root `style_guide.md` before coding (per FCT CLAUDE.md). No
  emojis. Blank `__init__.py`. No module-level `__all__`. Prefer `loguru`.
- **`edit-services` skill** documents the `services.toml` format; the new entry
  should match its conventions.
- The FCT CLAUDE.md `tk` task-tracking workflow applies to agents running *inside*
  the FCT container at runtime; the build agent (Claude Code in this monorepo
  workspace) uses its own task tools, not `tk`.
