# Caretaker/Scheduler — FROZEN interface contract

All phases are implemented in parallel off `origin/main` and merged sequentially (1→2→3→4 in the FCT repo, 5 in the minds monorepo). Because the phases are built before phase 1's code exists in their worktrees, **every cross-phase reference below is frozen**. Do not deviate from these names, paths, or signatures — if you think one is wrong, leave a clear note in your final report rather than changing it unilaterally.

Full design context: read `/Users/prestonseay/Desktop/mngr/blueprint/caretaker-scheduler/plan-caretaker-scheduler.md`.

## File / directory paths (frozen)

- `runtime/scheduled_tasks.toml` — the shared, human/agent-editable schedule.
- `runtime/scheduler/state.toml` — per-task last-run state.
- `runtime/scheduler/timezone` — single line, IANA tz name (e.g. `America/New_York`), written by minds at create time. Absent → fall back to host clock.
- `runtime/scheduler/logs/<task-name>.log` — captured stdout/stderr of each task run.
- `runtime/caretaker/<timestamp>.md` — one Caretaker run log per run. Timestamp format: `YYYY-MM-DDTHH-MM-SS` (filename-safe, colons replaced by dashes).
- `runtime/caretaker/preferences.toml` — Caretaker standing preferences.
- `.agents/skills/caretaker/scripts/run_caretaker.sh` — the wake entry point the scheduler invokes.

All `runtime/...` paths are relative to the repo root (`/mngr/code` in the container).

## `scheduled_tasks.toml` schema (frozen)

Top-level array of tables named `task`:

```toml
[[task]]
name = "caretaker"                                              # unique id
schedule = "0 3 * * *"                                          # standard 5-field cron (croniter syntax)
command = "bash .agents/skills/caretaker/scripts/run_caretaker.sh"   # arbitrary shell, run from repo root
enabled = true
catch_up = true                                                # run once on boot if missed during downtime
description = "Nightly Caretaker run: scans service logs and proposes fixes."
```

## `preferences.toml` schema (frozen)

```toml
auto_scan = false      # may scan logs without asking (unset/absent => ask)
auto_fix = false       # may apply fixes without asking (unset/absent => ask)
fix_scope = "minor_only"   # "minor_only" | "all" — how big a change the user allows
introduced = false     # set true only AFTER the first-run welcome is delivered
```
Consent fields are absent until the user answers; treat absent as "ask / not granted". `introduced` gates first-run detection.

## `scheduler` package (phase 1, frozen public surface)

FCT lib at `libs/scheduler/`, package import name `scheduler`.

- **One console script**: `scheduler = "scheduler.cli:main"` — a Click group.
  - `scheduler run` — the daemon loop (this is what supervisord runs: `uv run scheduler run`).
  - `scheduler list` — print current tasks (human + `--format json`).
  - `scheduler add --name N --schedule "CRON" --command "CMD" [--no-catch-up] [--disabled] [--description D]`
  - `scheduler remove NAME`
  - `scheduler show NAME`
- Data types in `scheduler.data_types`: `ScheduledTask{name,schedule,command,enabled,catch_up,description}`, `TaskRunState{name,last_run_at,last_exit_code,last_status}`.
- Engine in `scheduler.engine`: `compute_due_tasks(tasks, state, now, tz) -> list[ScheduledTask]` (pure; coalesces missed intervals into one; honors `catch_up`).
- File I/O in `scheduler.schedule_file` (tomlkit round-trip) and `scheduler.state`.

## Labels (frozen)

The Caretaker agent is created with `--label caretaker=true --label auto_created=true`.
- minds (phase 5) blinks any workspace whose labels include `auto_created=true` (equivalently `caretaker=true`) until first opened.

## Caretaker create template (frozen)

`.mngr/settings.toml` section `[create_templates.caretaker]` (type `claude`). `run_caretaker.sh` creates the agent with `--template caretaker`.

## Ownership map (no two phases touch the same file)

- **Phase 1** (me, FCT): `libs/scheduler/**` only.
- **Phase 2** (FCT): `supervisord.conf`, root `pyproject.toml`, `libs/bootstrap/src/bootstrap/manager.py`. Phase 2 seeds the **final** caretaker task (command = `bash .agents/skills/caretaker/scripts/run_caretaker.sh`) into `runtime/scheduled_tasks.toml` iff absent, and creates `runtime/scheduler/` + `runtime/caretaker/`. Phase 2 does NOT add a throwaway heartbeat task and does NOT touch any skill/template file.
- **Phase 3** (FCT): `.agents/skills/manage-scheduled-tasks/**`, `.agents/skills/check-app-errors/**`, `CLAUDE.md`.
- **Phase 4** (FCT): `.mngr/settings.toml`, `.agents/skills/caretaker/**` (SKILL.md, scripts/run_caretaker.sh, scripts/preferences.py, references/welcome-message.md). Phase 4 does NOT modify the bootstrap.
- **Phase 5** (minds monorepo): `apps/minds/imbue/minds/desktop_client/**` + `apps/minds/changelog/<branch>.md`.

## Changelogs

- Each FCT phase adds its own changelog entry file `changelog/<your-branch-name-with-slashes-as-dashes>.md` (so parallel branches never collide), briefly describing that phase's user-visible change.
- Phase 5 adds `apps/minds/changelog/<branch>.md` (and `dev/changelog/<branch>.md` only if it touches root/dev files).
