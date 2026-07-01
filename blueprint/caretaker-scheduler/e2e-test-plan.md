# End-to-end test plan: scheduler + Caretaker + minds blink

Exercises the whole feature as a real user would: create a workspace, let the
nightly Caretaker appear, watch the new tab blink, meet it, grant consent, and
confirm catch-up. Where waiting for real time (3 AM, a real reboot) is
impractical, the plan gives a time-compression shortcut that exercises the same
code path.

## Code under test (two branches, two repos)

- **FCT** `preston/nightly-agent-caretaker` (in the forever-claude-template repo):
  `libs/scheduler`, the `[program:scheduler]` service, the bootstrap seed, the
  `caretaker` template + skill + `run_caretaker.sh`, and the
  `manage-scheduled-tasks` / `check-app-errors` skills.
- **minds** `preston/nightly-agent-phase5-minds` (in the mngr monorepo,
  worktree `/Users/prestonseay/Desktop/mngr-phase5-minds`): the blinking-new-tab
  affordance + browser-timezone capture.

## 0. Prerequisites / bring-up

1. **Push the FCT branch** so the minds app can clone it: push
   `preston/nightly-agent-caretaker` to the forever-claude-template GitHub repo
   (or a fork you control). The desktop client creates agents by cloning the
   template git URL at a branch, so the new FCT code must be reachable remotely.
2. **Run the dev minds app from the phase-5 worktree** so the client carries the
   blink + timezone code. From `/Users/prestonseay/Desktop/mngr-phase5-minds`,
   follow the `minds-dev-workflow` skill: activate a dev env
   (`eval "$(uv run minds env activate <name>)"`), build assets
   (`just minds-css`), and `just minds-start`. Confirm `node_modules` is
   installed first (the worktree may be fresh).
3. **Point new workspaces at the FCT test branch.** Set
   `MINDS_WORKSPACE_BRANCH=preston/nightly-agent-caretaker` (and
   `MINDS_WORKSPACE_GIT_URL` if you pushed to a fork) before/at create time, so
   `mngr create` clones the branch under test rather than the released
   `minds-v*` tag.
4. Use the **docker** provider for the workspace (local, fast, has supervisord).

Sanity gate before functional testing: `just test-quick` for the touched minds
paths, and the FCT branch green in CI once pushed (`libs/scheduler` unit tests,
template-stacking, meta-ratchets).

## 1. Day 1 -- create a workspace (nothing new visible)

Create a workspace from the dev app. Then verify, inside the container
(`mngr exec`/terminal into the workspace, cwd `/mngr/code`):

- `supervisorctl status scheduler` -> `RUNNING`.
- `cat runtime/scheduled_tasks.toml` -> the seeded `caretaker` task at
  `0 3 * * *`, command `bash .agents/skills/caretaker/scripts/run_caretaker.sh`.
- `uv run scheduler list` -> shows that one task.
- `cat runtime/scheduler/timezone` -> the browser's IANA tz (e.g.
  `America/New_York`). (If absent, the create-time write didn't land -- see
  Troubleshooting.)
- `ls runtime/scheduler runtime/caretaker` -> both dirs exist, no logs yet.

In the UI: only the normal chat tab is present. **It must not blink.** No
Caretaker tab yet. This is the "user doesn't notice it on day 1" state.

## 2. Trigger the first nightly run (time-compressed)

Two equivalent options:

- **Direct (recommended first):** in the container, run
  `bash .agents/skills/caretaker/scripts/run_caretaker.sh`. This is exactly what
  the scheduler invokes at 3 AM. Watch its stdout: it should find no existing
  Caretaker and `mngr create caretaker ... --label caretaker=true --label
  auto_created=true`.
- **Through the scheduler (proves the timing path):** re-point the task to fire
  within a minute and let the service run it:
  `uv run scheduler add --replace --name caretaker --schedule "*/1 * * * *"
  --command "bash .agents/skills/caretaker/scripts/run_caretaker.sh"`. Because a
  *re-added* task is re-armed (it won't fire the instant it's added), wait for
  the next minute boundary, then the service fires it. Restore `0 3 * * *`
  afterward. Tail `/var/log/supervisor/scheduler-stdout.log` to watch the tick
  fire and `runtime/scheduler/logs/caretaker.log` for the wake output.

Verify after the wake:

- A new agent named `caretaker` exists with labels `caretaker=true`,
  `auto_created=true`, and the same `workspace=` label as the chat agent
  (`uv run mngr list --include 'labels.caretaker == "true"' --fields name,state`).
- `runtime/caretaker/preferences.toml`: `introduced = true` is **set only after**
  the welcome is delivered; `auto_scan` / `auto_fix` still unset (not granted).
- `runtime/caretaker/<timestamp>.md`: one run log exists, and it records a
  **cheap survey only -- no log scan** (the first run must not scan).

## 3. Day 2 -- the blinking tab + welcome (the UX core)

In the UI (test BOTH the Electron modal sidebar and browser-mode inline sidebar,
since the seen-store is wired into both):

- The new `caretaker` tab appears and **pulses in the workspace accent color**
  (the `pulse-accent` halo on its dot).
- The **day-1 chat tab does not pulse** (the seen-store seeded it as already-seen
  on first load).
- **Open the Caretaker tab:** the pulse stops immediately and permanently. Its
  chat shows the friendly, non-technical welcome (what it is, that it's
  configurable, and the two questions: may I scan? how big a fix may I make?).
- **Relaunch the app:** the Caretaker tab no longer pulses (seen-state persisted
  in `localStorage`); a still-unopened new tab, if any, would still pulse.

## 4. Consent -> a real nightly run

- Answer the welcome to grant scanning (and choose a `fix_scope`). Confirm
  `runtime/caretaker/preferences.toml` now has `auto_scan = true` (and the chosen
  `fix_scope`).
- Trigger another wake (Step 2). Verify it is the **same** Caretaker tab (no
  duplicate -- singleton by label), that it `/clear`ed, that it now **scans**
  (its new run log references service-log findings via `check-app-errors`), that
  it **read the previous run's log**, and that it wrote a **new** timestamped log.
- Plant a real problem first to make the scan meaningful: break a service (e.g.
  point a `[program:*]` command at a script that exits non-zero), let it log to
  `/var/log/supervisor/<name>-stderr.log`, and confirm the Caretaker surfaces it
  in plain language and proposes (or, if `auto_fix`+`fix_scope` allow, applies) a
  fix.

## 5. Catch-up after downtime (compressed with a heartbeat task)

Real reboot testing is slow; prove the catch-up path with a fast task:

- Add `uv run scheduler add --name heartbeat --schedule "*/2 * * * *" --command
  "date -u >> runtime/heartbeat.log"`. Let it run once (armed -> next fire).
- Stop the scheduler (`supervisorctl stop scheduler`), wait past two fire
  windows, then start it (`supervisorctl start scheduler`).
- Verify `runtime/heartbeat.log` gets **exactly one** new line shortly after
  restart (multiple missed fires coalesced into one), not one per missed window.
- Optionally flip it to `--no-catch-up` and repeat: after downtime it should
  write **nothing** until the next live fire. Remove the heartbeat task when done.

## 6. Singleton wake while busy

- Start a Caretaker run that will stay mid-turn (e.g. ask it, in its chat, to do
  something slow), then trigger another wake (Step 2) while its state is
  `RUNNING`.
- Verify `run_caretaker.sh` takes the busy branch: it sends the **wrap-up**
  message (asking it to finish its log and restart) rather than creating a second
  Caretaker. Confirm there is still exactly one Caretaker tab.

## 7. Schedule self-management (the skill)

- As the chat agent, drive the `manage-scheduled-tasks` skill: `scheduler list`,
  `scheduler add ...` a new task, `scheduler show`, `scheduler remove`. Confirm
  each change lands in `runtime/scheduled_tasks.toml` and takes effect within a
  minute with no restart.
- As the chat agent, drive `check-app-errors` against the broken service from
  Step 4 and confirm it surveys `supervisorctl status` + greps the logs and
  reports the failure with its log path.

## What "pass" means

- Day 1: scheduler running, schedule + timezone + dirs seeded, nothing blinks.
- The Caretaker is created lazily on its first wake, labelled correctly, and
  first appears (blinking) only after that -- i.e. "day 2".
- First run = welcome + cheap survey, no scan, `introduced` flips only post-welcome.
- Blink shows only on auto-created tabs, stops on open, persists across relaunch,
  never touches hand-made/day-1 tabs.
- Post-consent runs scan + read prior log + write a new log + propose/fix within
  `fix_scope`; the singleton is never duplicated (idle -> message; busy -> wrap-up).
- Catch-up runs a missed task exactly once on restart and coalesces multiple
  misses; `catch_up = false` suppresses backfill.

## Troubleshooting

- **No `runtime/scheduler/timezone`:** the create-time `mngr exec` write is
  best-effort on a detached retry thread; check the minds desktop-client logs for
  the write thread, and confirm `mngr exec <agent> ...` reaches the agent (the
  scheduler falls back to the host clock, so timing still works, just not in the
  user's tz).
- **Caretaker never appears:** read `runtime/scheduler/logs/caretaker.log` and
  `/var/log/supervisor/scheduler-stderr.log`; confirm `uv run mngr create` works
  from inside the container with the workspace's settings (the host-only
  `isolate_local_config_dir` skew seen on the dev Mac does not occur in-container).
- **Tab doesn't blink:** confirm `_build_workspace_list` emitted `is_new` (check
  the agent's labels) and that `seen_workspaces.js` didn't already seed that id
  (clear the `minds.seenWorkspaceIds*` localStorage keys to re-test a fresh
  client).
