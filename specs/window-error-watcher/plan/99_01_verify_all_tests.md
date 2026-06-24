# Task 99.1: Run all tests added in this plan and iterate to green

## Goal

Run every test introduced or modified by this plan and iterate until they all
pass. This is a safety check after the per-task work — even though each task
verified its own scope, cross-task interactions may have introduced regressions.

## Background

This is the second-to-last task in the plan. By now the watcher lib
(`libs/error_watcher/`) is implemented, its loop is wired, and it is registered
as a service in `services.toml`. All of this lives in the **forever-claude-template
(FCT) clone** at `.external_worktrees/forever-claude-template/`, git branch
`preston/error-checker`. Per-task verification has already passed, but this task
runs the affected suites together.

The spec and plan live in the monorepo at `specs/window-error-watcher/`; they
contain no code and need no test run.

## Files to modify/create

None expected. If you find a failure, fix it in the FCT source file the failure
originates from (and commit that fix in the FCT clone).

## Implementation details

All commands run from inside the FCT clone
(`.external_worktrees/forever-claude-template/`).

1. Run the new lib's full suite (unit + integration + ratchet), WITHOUT the
   coverage-skip flags (CI enforces coverage):
   `cd libs/error_watcher && uv run pytest`
2. Run the bootstrap suite too, since `services.toml` changed and bootstrap
   parses it: `cd libs/bootstrap && uv run pytest`
3. If anything fails: debug, fix the source, re-run. Iterate until green. The
   loop must never crash — if a test exposes an unguarded subprocess/tmux call,
   fix it (REQ-SPAWN-4).
4. Ratchet snapshots: if `test_error_watcher_ratchets.py` fails because counts
   changed, regenerate with
   `uv run pytest --inline-snapshot=create libs/error_watcher/test_error_watcher_ratchets.py`
   (run WITHOUT xdist so inline-snapshot is active), then re-run normally.
5. Confirm the console script still resolves: `uv run error-watcher` starts
   (Ctrl-C to stop).

## Verification checklist

- [ ] `cd libs/error_watcher && uv run pytest` passes (no `--no-cov`).
- [ ] `cd libs/bootstrap && uv run pytest` passes.
- [ ] Ratchet snapshots reflect the real counts for `libs/error_watcher/`.
- [ ] `uv run error-watcher` resolves and starts.
- [ ] Report the exact pytest command(s) used and the passed/failed counts
  (failed must be 0).

## Commit policy

**Do NOT make an empty commit.** If everything passed first try, report success
without a commit. If you fixed regressions, commit those fixes in the FCT clone
on branch `preston/error-checker`, with a message ending in:

```
Co-authored-by: Sculptor <sculptor@imbue.com>
```
