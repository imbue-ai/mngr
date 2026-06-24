# Task 3.1: Register the service in `services.toml`, add docs + changelog

## Goal

Turn the working watcher into a service the bootstrap manager owns: add a
`[services.error-watcher]` entry to `services.toml`, write the lib README, add
the FCT changelog entry, and verify end-to-end that the bootstrap manager spawns
the watcher in a `svc-error-watcher` window.

## Requirements addressed

REQ-SPAWN-1 (service registration), REQ-SPAWN-4 (restart policy).

## Background

### What this feature is

The Window Error Watcher is a forever-claude-template (FCT) background service
that polls every tmux window in its session for `/error|exception/i` output and
messages a random mngr agent on new matches.

### Where the code lives (critical)

- Built in the **FCT clone** at `.external_worktrees/forever-claude-template/`
  (relative to the monorepo root), git branch `preston/error-checker`. That
  directory is gitignored by the monorepo and is its own git repo — commit there.
- All paths below are relative to the FCT clone root.

### What earlier tasks produced

- Task 1.1 created `libs/error_watcher/` with the console script
  `error-watcher = "error_watcher.watcher:main"` and the pure core
  (match/dedup/format/argv/parse/choose), unit-tested.
- Task 2.1 implemented `main()` — the 5s poll loop that scans windows (skipping
  its own `svc-error-watcher` window), and on new matches sends one batched
  message to a random messageable mngr agent. It is runnable via
  `uv run error-watcher`.

### The services model to follow

- `services.toml` (FCT root) lists `[services.<name>]` entries, each with a
  `command = "..."` and optional `restart = "on-failure"`. The bootstrap manager
  (`libs/bootstrap/src/bootstrap/manager.py`) reads this file, runs each service
  in a tmux window named `svc-<name>`, watches the file for changes, and
  reconciles (start new, stop removed). The header comment in `services.toml`
  documents the format; the `edit-services` skill
  (`.agents/skills/edit-services/` or `.claude/skills/edit-services/`) is the
  authoritative guide — read it before editing.
- Existing entries to match style: `[services.app-watcher]` is
  `command = "uv run app-watcher"`, `restart = "on-failure"`.

### FCT changelog convention

FCT uses a single top-level `changelog/` directory with one `<branch-name>.md`
file per branch (slashes → dashes). Existing examples:
`changelog/aws-minds-compute-provider.md`. For branch `preston/error-checker`,
the file is `changelog/preston-error-checker.md`.

## Files to modify/create

(All under the FCT clone, branch `preston/error-checker`.)

- `services.toml` — modify: add the `[services.error-watcher]` entry.
- `libs/error_watcher/README.md` — modify: expand to a real description (what it
  watches, the `/error|exception/i` match, the random-agent alert, the 5s
  cadence, that it skips its own window, the in-memory dedup caveat across
  restarts).
- `changelog/preston-error-checker.md` — new: short user-facing description of
  the new `error-watcher` service.

## Implementation details

1. **Register the service.** Add to `services.toml`, with a brief comment in the
   same style as the surrounding entries:

   ```toml
   # Scans every tmux window in the session for /error|exception/i output and,
   # on newly-appeared matches, sends one message to a random mngr agent. Skips
   # its own window. See libs/error_watcher/README.md.
   [services.error-watcher]
   command = "uv run error-watcher"
   restart = "on-failure"
   ```

   `restart = "on-failure"` gives REQ-SPAWN-4 a backstop: if the loop ever does
   exit non-zero, bootstrap relaunches it.

2. **README.** Write `libs/error_watcher/README.md` covering purpose, the match
   pattern, the random-agent notification, the 5s poll, the own-window exclusion,
   and the Non-Goals (naive matching; in-memory dedup that may re-alert after a
   restart). Keep it concise and emoji-free.

3. **Changelog.** Write `changelog/preston-error-checker.md` — one short
   paragraph describing the new `error-watcher` service from a user's
   perspective.

## Testing suggestions

- Sanity: `cd libs/error_watcher && uv run pytest` still passes.
- Confirm `uv run error-watcher` resolves from the FCT root (the console script
  is installed in the workspace venv).
- If the bootstrap manager has unit tests over `services.toml` parsing, run them:
  `cd libs/bootstrap && uv run pytest` (the reconciliation logic is pure and
  tested in `manager_test.py`).

### Manual end-to-end verification (NOT crystallized into pytest)

Per the FCT CLAUDE.md tmux-verification guidance, verify by hand and do not turn
it into a pytest test:

1. From a tmux session in the FCT clone, run the bootstrap manager
   (`uv run bootstrap`) — or, if running the full manager is impractical locally,
   manually create the window the way it would: `tmux new-window -n
   svc-error-watcher 'uv run error-watcher'`.
2. Confirm a `svc-error-watcher` window appears and the watcher is polling.
3. In another window, print a fake traceback; confirm the watcher logs a match
   and attempts a `mngr message`.
4. Remove the `[services.error-watcher]` entry (if testing the full manager) and
   confirm bootstrap closes the window (Scenario 5).

## Gotchas

- The service `command` runs with cwd = repo root (`/code`), per FCT CLAUDE.md
  ("All relative paths in this repo assume cwd = repo root"). `uv run
  error-watcher` resolves the console script from the workspace — no path
  juggling needed.
- Do not edit `libs/web_server/` or other example placeholders.
- Keep the `services.toml` comment style consistent with the existing entries
  (a short `#` comment block above the entry).
- This is a contract-bearing config change; if the FCT `edit-services` skill
  prescribes a specific procedure, follow it.

## Verification checklist

- [ ] `services.toml` contains a `[services.error-watcher]` entry with
  `command = "uv run error-watcher"` and `restart = "on-failure"`, with a
  style-matching comment.
- [ ] `libs/error_watcher/README.md` describes the service accurately.
- [ ] `changelog/preston-error-checker.md` exists and describes the change.
- [ ] `uv run error-watcher` resolves and starts from the FCT root.
- [ ] Manual end-to-end check performed: bootstrap (or a manual `new-window`)
  spawns `svc-error-watcher`, it detects an injected error, and attempts an
  alert.
- [ ] End-to-end tests: bootstrap's `services.toml` reconciliation unit tests
  (`libs/bootstrap/src/bootstrap/manager_test.py`) still pass; real-tmux E2E is
  intentionally manual per FCT convention.

## Commit policy

Commit this task's work in the FCT clone on branch `preston/error-checker` with a
descriptive message ending in:

```
Co-authored-by: Sculptor <sculptor@imbue.com>
```

**Do NOT make an empty commit.** If nothing changed (everything was already in
place), report success without committing.
