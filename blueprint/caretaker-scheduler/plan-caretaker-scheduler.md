# Plan: Scheduled tasks + the Caretaker nightly agent

## Refined prompt

> I want the following system added to minds. (Note: some of these changes may be done in the forever-claude-template.)
>
> 1) A system for regularly running tasks (e.g., cron, but a way for the agents to keep track of it and manage it).
> 2) A nightly agent template that will spawn an agent to do nightly tasks.
> 3) Improved skills for the current agents to know that they should always be checking their apps for errors, pointing them at the current logs. Also a new skill for the nightly agent to know how what it is supposed to do.
> 4) A nice onboarding interface that will create a new chat that shows the user that it is there and provides clear instructions for how it works.
>
> **[REGULAR TASKS]**
> * The regular tasks can be run with cron, but there should be a file on the system that shows the agents on the vm what is currently scheduled.
> * There should be an easy way for agents to modify this file in a consistent manner and query it (e.g., a skill as listed below).
> * The tasks should run automatically if the computer was offline when it was supposed to run (e.g., if it's supposed to run hourly and shut off for 3 hours, then it should run once when started up).
> * Timing is owned by a thin file-driven scheduler service; the shared, agent-editable schedule lives at `runtime/scheduled_tasks.toml`, with per-task last-run state at `runtime/scheduler/state.toml` so catch-up survives reboots.
> * Catch-up coalesces multiple missed intervals into a single run; the scheduler ticks once per minute.
> * Scheduled tasks run arbitrary shell commands (fields: `name`, `schedule`, `command`, `enabled`, `catch_up`, `description`); the Caretaker entry is just a `mngr` command like any other.
> * The FCT bootstrap seeds a default `scheduled_tasks.toml` on first boot containing the Caretaker's 3 AM entry, which the user can edit (change the time) or disable.
> * Schedules run in the user's local timezone, captured once from the desktop client and stored for the scheduler (falling back to the host clock if unknown).
>
> **[NIGHTLY AGENT]** (the **Caretaker**)
> * The nightly agent should do the following when run: (1) /clear all of the context, (2) scan all of the supervisor'd services logs and check for errors, (3) read the report of the previous nightly agent, (4) plan good changes to fix any outstanding issues in the VM workspace, (5) make a proposal to the user explaining what the nightly agent is and what it can do (i.e., it should ask the user if they want it to perform fixes, speaking in a non-technical way, grounded in user-experience).
> * Runs once every night at 3 AM by default; named the **Caretaker**.
> * Not pre-created at boot; the 3 AM scheduler creates it on its first run, so the user first sees it on day 2.
> * On later nights it wakes the singleton: interrupt the running Caretaker to finish by writing its run log, then have it `mngr message` itself to `/clear` and start the new day's routine.
> * Each run is logged at `runtime/caretaker/<timestamp>.md`; it keeps the most recent 30 logs (pruning older) but reads only the single latest log for continuity.
> * `runtime/caretaker/preferences.toml` holds two toggles, `auto_scan` and `auto_fix`, both defaulting to ask; first run does only a cheap capability survey + welcome, scans logs only once `auto_scan` is granted, applies fixes only once `auto_fix` is granted.
> * By default it only takes low-risk actions (restart a crashed service, edit config) and hands off code changes via a task/message; it may ask for broader permission, always in non-technical, user-experience terms.
> * If the user never answers, it keeps gently re-proposing what it can do each night (doing only the cheap survey); the user can stop it entirely by disabling/removing its scheduled task.
>
> **[IMPROVING CURRENT AGENTS]**
> * Ensure that there are instructions that tell the current agents to be checking for any errors in the apps that they create.
> * Passive: a CLAUDE.md instruction plus a `check-app-errors` skill that surveys all services via `supervisorctl status`, scans `/var/log/supervisor/*` for errors/tracebacks, summarizes what's wrong, and proposes helper commands (ls/grep, etc.) to locate issues; the Caretaker reuses it for its step-2 scan.
>
> **[WORKING WITH SCHEDULED TASKS SKILL]**
> * There should be a skill telling the agents how to interact with the scheduled tasks file & cron.
>
> **[NIGHTLY AGENT SKILL]**
> * There should be clear skills that tell the nightly agent how to do each of the tasks its supposed to do (as listed above).
>
> **[USER EXPERIENCE]**
> * The user should not notice this the first day, but on their second day they should see a new tab that is blinking their highlight color to indicate that it is new.
> * Only agents carrying an `auto_created`/`caretaker` label blink (accent color) until first opened; hand-made agents and the day-1 chat tab never blink.
> * When the user clicks on the new chat, it should have a message from the nightly agent that welcomes the user to what the nightly agent is doing or has done or what it is proposing (have it by default show the new tab and work on identifying things that it can do, but then ping the user when it is ready to start looking at logs -- we don't want the user to be surprised that their tokens are being used).
> * The Caretaker is self-introducing — the day-1 chat stays clean; its own tab and welcome message are the sole introduction.
> * The welcome message plainly invites the user to reschedule it, add new scheduled tasks/agents, or shut it down, all in non-technical language.
> * Notification is in-app only for now (blinking tab + first chat message); a louder `send-user-message` ping is noted as a possible later addition.
> * It should show a friendly message to them that says what the nightly agent is and ALSO explaining that the agent is completely configureable and the user can request more regularly scheduled tasks & agents.
> * Testing: thoroughly unit-test the scheduler (due/overdue/coalesce/last-run persistence), add a focused minds test for label-based blink + seen-tracking, and verify the Caretaker routine manually plus a light smoke test.
> * The plan spans two repos — FCT (scheduler service, skills, Caretaker template, CLAUDE.md) and the minds app (label-based blink + seen-tracking) — each committed and changelogged in its own repo; FCT work starts from a fresh worktree off `origin/main`.

---

## Overview

- Add a self-managing **scheduled-task system** plus a nightly **Caretaker** agent to the forever-claude-template (FCT), and a small **blinking-new-tab** affordance to the minds desktop app.
- The work spans two repos: the FCT repo (`.external_worktrees/forever-claude-template/`, started fresh off `origin/main`) holds the scheduler, skills, Caretaker template, and CLAUDE.md edits; the minds monorepo (`apps/minds/`) holds the UI blink.
- **Build a thin file-driven scheduler, not cron.** Plain cron and `mngr schedule` (which sits on cron) never re-run jobs missed during downtime, and `anacron` only supports daily granularity. We own timing ourselves so we can (a) catch up missed runs on boot and (b) expose one human/agent-readable schedule file.
- **One readable, agent-editable schedule file** at `runtime/scheduled_tasks.toml`, with last-run state in `runtime/scheduler/state.toml`. `runtime/` is gitignored but per-agent backed up and shared by every agent on the host, so it is the natural home for shared mutable state.
- **The Caretaker is a singleton nightly agent**, created lazily by its first scheduled run (so it first appears on day 2), then re-woken each night by the scheduler (interrupting it only if it is still mid-turn). It is conservative by default (consent-gated scanning and fixing) so the user is never surprised by token use.
- **Skills carry the behavior, not bespoke code.** Three new skills (`manage-scheduled-tasks`, `caretaker`, `check-app-errors`) plus CLAUDE.md edits teach agents to use the schedule file and watch their apps for errors. The Caretaker template just points a `claude`-type agent at the `caretaker` skill.
- **minds stays a launcher.** It only learns to render a new agent's `caretaker`/`auto_created` label as a pulsing accent until first opened (client-side "seen" tracking). The welcome chat is produced by the Caretaker itself, not by minds.

## Expected behavior

- **Day 1:** the user creates their workspace and chats as today. Nothing new is visible; the day-1 chat tab never blinks.
- **Overnight (3 AM, user's local time):** the scheduler fires the seeded `caretaker` task. No Caretaker exists yet, so the task runs `mngr create caretaker@<host>` with labels `caretaker=true` + `auto_created=true`. The first run does only a cheap capability survey (enumerate supervised services it could watch) and writes its first log + a welcome message; it does **not** scan logs (consent ungranted).
- **Day 2 morning:** the user sees a new tab pulsing in their workspace accent color. Opening it stops the pulse and shows the Caretaker's friendly, non-technical welcome: what it is, what it could do, that it is fully configurable (reschedule, add tasks/agents, or shut it down), and a request for permission to start looking at logs.
- **After consent (`auto_scan`):** each subsequent 3 AM run scans `/var/log/supervisor/*` thoroughly for errors, reads the previous run's log, and proposes fixes in user-experience terms. What it does itself is bounded by `auto_fix` + `fix_scope`: with `minor_only` it restarts crashed services / edits config and hands off bigger changes; with `all` it can take on larger fixes too. Without `auto_fix` it only proposes.
- **No consent ever:** the Caretaker keeps doing only the cheap survey and gently re-proposing each night; it never burns tokens scanning. The user can disable it by editing `runtime/scheduled_tasks.toml` (or asking an agent to).
- **Offline catch-up:** if the machine was off at 3 AM, the scheduler runs the overdue Caretaker task once shortly after the next boot. Multiple missed intervals for any task collapse into a single catch-up run.
- **Singleton wake:** on later nights the scheduler messages the existing Caretaker its new-day routine (which begins with `/clear`). If the prior run is idle, that's a clean clear-and-restart (its incremental log is already complete). If it's still mid-turn, the wake instead asks it to finish its log and restart itself; a hard restart (`mngr start caretaker --restart --no-resume`) is only the backstop for an unresponsive run. Either way there is never a second Caretaker tab.
- **Any agent can manage the schedule:** the chat agent (or user, via the agent) can add/edit/remove tasks in `runtime/scheduled_tasks.toml` through the `manage-scheduled-tasks` skill; changes take effect within a minute, no restart needed.
- **Day-to-day agents watch their apps:** after building or editing a service, a chat agent is reminded (via CLAUDE.md + the `check-app-errors` skill) to survey `supervisorctl status` and the relevant logs for errors.

## Implementation plan

### A. FCT repo — scheduler service (`libs/scheduler/`)

New lib modeled on `libs/app_watcher/` (a long-running supervised Python service).

- `libs/scheduler/pyproject.toml` — package `scheduler`, console script `scheduler = "scheduler.runner:main"`; deps: `tomlkit` (round-trip TOML edits), `croniter` (cron parsing + prev/next fire-time math), `pydantic`, `loguru`. (`croniter` is already trusted in the monorepo via `rq`, and its only dependency, `python-dateutil`, is already in the FCT lock, so it adds just one package.)
- `libs/scheduler/src/scheduler/data_types.py`
  - `ScheduledTask` (pydantic `FrozenModel`): `name: str`, `schedule: str` (cron expr), `command: str` (arbitrary shell), `enabled: bool = True`, `catch_up: bool = True`, `description: str = ""`.
  - `ScheduleFile`: `tasks: tuple[ScheduledTask, ...]` — the parse of `scheduled_tasks.toml`.
  - `TaskRunState`: `name: str`, `last_run_at: datetime | None`, `last_exit_code: int | None`, `last_status: Literal["ok","error","running"]`.
- `libs/scheduler/src/scheduler/schedule_file.py`
  - `read_schedule(path) -> ScheduleFile` and `write_task(path, task)` / `remove_task(path, name)` using `tomlkit` so comments/formatting survive agent + programmatic edits. These functions are what the `manage-scheduled-tasks` skill shells out to (via a tiny CLI, below).
  - `resolve_timezone() -> tzinfo` — read `runtime/scheduler/timezone` (written by minds at create time); fall back to the host clock.
- `libs/scheduler/src/scheduler/state.py`
  - `load_state(path) -> dict[str, TaskRunState]`, `save_state(path, state)` over `runtime/scheduler/state.toml` (atomic write: temp file + rename).
- `libs/scheduler/src/scheduler/engine.py` — pure, unit-testable timing core (no I/O, no clock of its own):
  - `compute_due_tasks(tasks, state, now, tz) -> list[ScheduledTask]`: for each enabled task, use `croniter` to find the most recent fire time `<= now`; it is due iff that fire time is strictly after `last_run_at` (or `last_run_at is None`). Because we only consider the single most recent fire time, multiple missed intervals naturally **coalesce into one** run. `catch_up=False` tasks are due only if that most recent fire time falls within the current tick window (no back-fill).
  - `next_state_after_run(state, task, now, exit_code)`: stamp `last_run_at=now`.
  - The supported cron syntax is whatever `croniter` accepts (standard 5-field); the `manage-scheduled-tasks` skill documents the common forms.
- `libs/scheduler/src/scheduler/runner.py` — the service entry point:
  - `main()`: loop forever; each tick (every 60 s) read schedule + state, `compute_due_tasks(now=datetime.now(tz))`, run each due task's `command` via `subprocess` (from `/mngr/code`, inheriting the agent env), capture output to the task's rotating log under `runtime/scheduler/logs/<name>.log`, then persist state. On startup it runs one immediate tick so boot-time catch-up happens without waiting a minute.
  - Concurrency guard: skip launching a task whose previous invocation is still `running` (tracked in state) to avoid pile-ups.
- `libs/scheduler/src/scheduler/cli.py` — thin Click CLI (`scheduler add|list|remove|show`) so skills/agents have a consistent, validated way to edit the file rather than hand-writing TOML. Console script `scheduler-cli`.
- Unit tests: `libs/scheduler/src/scheduler/engine_test.py`, `schedule_file_test.py`, `state_test.py`.

### B. FCT repo — wire the service in

- `supervisord.conf`: add `[program:scheduler]` mirroring `[program:app-watcher]` (`command=uv run scheduler`, `directory=/mngr/code`, autostart/autorestart, logs to `/var/log/supervisor/scheduler-{stdout,stderr}.log`).
- Root `pyproject.toml`: add `scheduler` as a workspace member + dependency + uv source mapping (same three edits the `build-web-service` skill makes for new libs).
- `libs/bootstrap/src/bootstrap/manager.py`: in first-boot setup (alongside the `runtime/` worktree init, near `INITIAL_CHAT_SIGNAL`), seed a default `runtime/scheduled_tasks.toml` **iff absent**, containing a single commented, human-readable `caretaker` task: `schedule = "0 3 * * *"`, `command = "bash .agents/skills/caretaker/scripts/run_caretaker.sh"`, `catch_up = true`, with a description explaining it. Create `runtime/scheduler/` and `runtime/caretaker/`.

### C. FCT repo — Caretaker agent

- `.mngr/settings.toml`: add `[create_templates.caretaker]` modeled on `[create_templates.chat]` (`type = "claude"`), with `agent_args` appending a system prompt that tells the agent it is the Caretaker and must follow the `caretaker` skill, and `env = ["MNGR_AGENT_ROLE=caretaker"]`.
- `.agents/skills/caretaker/` (the nightly routine skill):
  - `SKILL.md` — the routine as explicit, ordered instructions. **First-run detection:** treat it as the first run unless `preferences.introduced` is true. Steps: (1) `/clear`, then open today's `runtime/caretaker/<timestamp>.md` and write to it **incrementally** as the run proceeds, so an interruption never loses more than the last step; (2) reuse `check-app-errors` to scan supervised logs **only if `auto_scan` granted** — and when granted, scan thoroughly, relying on the skill's efficient search commands to stay cheap (no explicit token budget); skip scanning on the first run; (3) read the single latest *prior* log for continuity; (4) plan fixes scoped to the user's stated comfort (`fix_scope`: `minor_only` vs `all`), applying only within granted `auto_fix` + `fix_scope` and handing off anything bigger; (5) post/update the user proposal in non-technical, user-experience language — on the first run this is the welcome + cheap capability survey that also asks what kinds of changes it may make (tidy small things vs bigger fixes), framed around its value as a caretaker of the user's "mind"; set `preferences.introduced = true` only after the welcome is delivered; then prune logs to the newest 30. **On a wake/wrap-up message mid-run:** finish writing the current log, then restart itself for the new day. Encodes the consent model (`auto_scan`, `auto_fix`, `fix_scope`, all default to ask).
  - `scripts/run_caretaker.sh` — the idempotent wake entry point the scheduler calls. Find an existing Caretaker via `mngr` (label `caretaker=true`).
    - **None exists** → create it, mirroring the bootstrap's `_build_create_chat_command` shape: `mngr create <host> --transfer none --template caretaker --no-connect --format json --label caretaker=true --label auto_created=true --message <nightly-routine>`.
    - **Exists, idle/stopped** → `mngr message caretaker "<nightly-routine>"` (it picks it up; the routine starts with `/clear`). Its prior log is already complete (written incrementally), so a clean clear-and-restart is safe.
    - **Exists, still mid-turn** → send a graceful wrap-up message asking it to finish its log and restart itself for the new day. Incremental logging keeps the log safe regardless. Backstop: if it's still mid-turn on the next tick (unresponsive), hard-restart via the chat UI's interrupt process (`mngr start caretaker --restart --no-resume`) then re-message.
  - `scripts/preferences.py` — read/write helper for `runtime/caretaker/preferences.toml`: `auto_scan` (may scan logs), `auto_fix` (may apply fixes), `fix_scope` (`minor_only` | `all` — how big a change the user is comfortable with), and `introduced` (set true only after the first-run welcome is delivered). Consent fields default to ask/unset so the skill reads them deterministically.
  - `references/welcome-message.md` — the canonical friendly welcome text (what it is, it's configurable, how to reschedule/add tasks/shut it down, and what kinds of changes it may make — minor tidy-ups vs bigger fixes).

### D. FCT repo — skills for all agents + CLAUDE.md

- `.agents/skills/manage-scheduled-tasks/SKILL.md` — how to query and edit `runtime/scheduled_tasks.toml`: read with `scheduler list`, add/remove with the `scheduler` CLI (never hand-edit while the service may be writing), the schema and cron syntax, catch-up semantics, and that changes apply within a minute.
- `.agents/skills/check-app-errors/SKILL.md` — survey all services with `supervisorctl status`, scan `/var/log/supervisor/*-stderr.log` (and stdout) for errors/tracebacks, summarize, and propose helper commands (`ls`, `grep -nE 'Traceback|ERROR|Exception'`, `tail`) to locate issues fast. Written to be reusable by both day-to-day agents and the Caretaker's step 2.
- `CLAUDE.md` (FCT root): add a short instruction under the services/web-app guidance telling agents to proactively check `/var/log/supervisor/` for errors after building or editing any app/service, pointing at the `check-app-errors` skill.

### E. minds repo — blinking new tab (`apps/minds/`)

- `imbue/minds/desktop_client/backend_resolver.py`: add `get_agent_labels(agent_id) -> dict[str, str]` mirroring `get_workspace_color` (iterate `self._agents_result.discovered_agents`, return matching `agent.labels`). Labels already flow through `DiscoveredAgent.labels`.
- `imbue/minds/desktop_client/app.py` `_build_workspace_list` (~2515-2569): when the agent's labels contain `auto_created=true` (or `caretaker=true`), add `entry["is_new"] = "true"`. (`create_time` is already available via `AgentDisplayInfo.create_time` if we later want age-based logic; label-based is the chosen mechanism.)
- `imbue/minds/desktop_client/static/sidebar_workspace_row.js` `buildRow`: when `workspace.is_new` and the id is not in the client "seen" set, add a `sidebar-new` class to the row (mirroring the existing `is-stale` conditional).
- `imbue/minds/desktop_client/static/chrome.js`: add a persisted `seenWorkspaceIds` set backed by `localStorage`; seed it with all currently-known ids on a fresh client so existing tabs never blink; mark an id seen (and re-render) when the user opens that workspace (in the row click handler / `setDisplayedWorkspaceAgentId`). Pass the seen-set into `renderWorkspaces`/`buildRow`.
- `imbue/minds/desktop_client/static/app.css`: add `@keyframes pulse-accent` (mirror the existing `@keyframes spin`) and `.sidebar-new .sidebar-dot { animation: pulse-accent 1.4s ease-in-out infinite; }` (pulse the accent dot / row using `var(--workspace-accent)`).

### F. minds repo — capture the user's timezone

- `imbue/minds/desktop_client/onboarding.py` (or the agent-creation `on_created` path in `agent_creator.py`): capture the desktop client's IANA timezone (browser `Intl.DateTimeFormat().resolvedOptions().timeZone`, sent with the create form) and write it into the new host's `runtime/scheduler/timezone` (one line) so the scheduler resolves "3 AM" in the user's local time. Fall back to the host clock when absent.

### G. Changelogs (both repos)

- minds monorepo: `apps/minds/changelog/<branch>.md`. If any root/dev files change, also `dev/changelog/<branch>.md`.
- FCT repo: its own changelog convention (per the FCT repo's CONTRIBUTING/CLAUDE.md).

## Implementation phases

Each phase leaves a working (if incomplete) system.

1. **Scheduler core (FCT).** Build `libs/scheduler/` (`data_types`, `engine`, `schedule_file`, `state`, `runner`, `cli`) with full unit tests for the timing/catch-up logic. No supervisord wiring yet — runnable via `uv run scheduler` by hand against a sample file. *Verifiable: unit tests + manual tick.*
2. **Wire scheduler into the FCT runtime.** Add `[program:scheduler]`, the root `pyproject.toml` edits, and bootstrap seeding of `runtime/scheduled_tasks.toml` + dirs (with a harmless sample task, e.g. a `date >> log` heartbeat, not yet the Caretaker). *Verifiable: boot an FCT agent, see the heartbeat task run and catch up after a simulated downtime.*
3. **Scheduled-tasks + error-checking skills (FCT).** Add `manage-scheduled-tasks` and `check-app-errors` skills and the CLAUDE.md instruction. *Verifiable: an agent can add/list/remove a task and run an error survey.*
4. **Caretaker agent (FCT).** Add the `caretaker` skill (routine + `run_caretaker.sh` + `preferences.py` + welcome text), the `[create_templates.caretaker]` template, and switch the seeded default task to the real Caretaker 3 AM entry. *Verifiable: manually invoke `run_caretaker.sh` → a Caretaker agent is created with the right labels, does the cheap survey, writes a log, posts the welcome; invoke again → singleton wake (plain message when idle; graceful wrap-up message when busy), no duplicate.*
5. **minds blink + timezone (minds repo).** `get_agent_labels`, `_build_workspace_list` `is_new`, `buildRow` class, `chrome.js` seen-tracking + localStorage, `app.css` keyframes, timezone capture. *Verifiable: a labeled agent pulses until opened; the day-1 chat tab never pulses; `runtime/scheduler/timezone` is written.*
6. **End-to-end + polish.** Run the full two-repo flow against a dev Docker agent (per `minds-dev-workflow`): fresh workspace → overnight Caretaker creation → day-2 blink → welcome → consent → nightly scan. Add changelogs in both repos. Tighten any ratchets touched.

## Testing strategy

- **Scheduler engine (unit, thorough):** `engine_test.py` — first-run-with-no-state is due; not-yet-due is skipped; a single missed interval runs once; **three missed hourly intervals coalesce into one** run; `catch_up=False` does not back-fill; disabled tasks never run; timezone is respected (3 AM local vs UTC); `last_run_at` advances correctly. Inject `now` and `tz` so tests are deterministic (no real clock).
- **Schedule file + state (unit):** `schedule_file_test.py` round-trips TOML preserving comments; add/remove are idempotent; malformed entries raise clearly. `state_test.py` covers atomic write + reload and the `running` concurrency guard.
- **CLI (unit):** `cli_test.py` — `add/list/remove/show` produce the expected file mutations and output.
- **minds blink (focused):** a unit/integration test that `_build_workspace_list` sets `is_new` exactly when an agent carries `auto_created`/`caretaker` and not otherwise; a light DOM/JS test (or extend `test_desktop_client_e2e.py`) that a `sidebar-new` row pulses and that opening it clears the class and persists "seen".
- **Caretaker routine (manual + light smoke):** full agent runs are not cheap in CI, so verify manually via `minds-dev-workflow` (first run = cheap survey + welcome, no scan; consent flips behavior per `auto_scan`/`auto_fix`/`fix_scope`; singleton wake produces no duplicate tab; logs prune to 30, read latest). Add a smoke test that `run_caretaker.sh` selects the right branch — create (none exists) / plain message (idle) / graceful wrap-up message (busy), with hard-restart backstop — against a mocked `mngr` (no real agent spawned).
- **Edge cases to assert:** machine offline across several 3 AM boundaries → exactly one catch-up run; a still-running Caretaker at next fire → asked to wrap up and self-restart (hard-restart backstop), not duplicated; a first run that crashes before delivering the welcome → still treated as first run next time (`introduced` unset); missing `timezone` file → host-clock fallback; empty/absent `scheduled_tasks.toml` → scheduler idles without crashing; consent never granted → no log scanning, proposal re-surfaces.
- **Full suite:** `just test-offload` from the monorepo root for the minds changes; FCT changes validated in the FCT repo's CI; report exact command + pass/fail counts before finishing.

## Open questions

Resolved during refinement (recorded for the record):
- **Wake/interrupt:** idle → plain `mngr message` (clear-and-restart). Mid-turn → ask it to finish its log and self-restart (incremental logging keeps the log safe), with the chat UI's hard interrupt (`mngr start --restart --no-resume`) as the backstop for an unresponsive run.
- **In-container `mngr create`** reuses the bootstrap's `_build_create_chat_command` shape (`--transfer none --template caretaker --no-connect --format json` + labels + message).
- **First-run detection** uses an explicit `introduced` marker in `preferences.toml`, set only after the welcome is delivered, so a crashed first run is correctly retried.
- **`croniter`** is added as an explicit FCT dependency (already trusted in the monorepo via `rq`; its only dep `python-dateutil` is already in the FCT lock, so it adds one package).
- **Timezone** is captured once at create time; it won't follow the user if they change timezones (acceptable for v1).
- **Caretaker restraint** is soft (skill-enforced) plus a user-set `fix_scope` (`minor_only` | `all`) it asks about in its proposal, framed as a caretaker of the user's "mind."
- **No explicit scan budget** — when `auto_scan`/`auto_fix` are granted it acts thoroughly, staying cheap via the `check-app-errors` skill's efficient search commands.
- **`mngr_schedule`** is out of scope; we build our own minimal scheduler and do not touch it.

Genuinely open: none remaining — all decisions are resolved. (Implementation will still confirm the exact bootstrap invocation shape and `mngr` lifecycle-state query against the real binary, but these are verification steps, not design unknowns.)

✓ Explore  ✓ Plan  ● Write  ○ Refine
