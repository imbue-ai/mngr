# Cron automation recipes

Ideas for **recurring** usage-driven automation: let `cron` poll
`mngr usage --format json` on a schedule and act when usage looks a certain way.
For a **one-off**, see [Waiting on a predicate](../README.md#waiting-on-a-predicate).

Across the usage-driven recipes below:

- `mngr usage --format json` always prints a `sources` array (empty on a
  no-data tick), so a `jq` predicate that matches nothing yields no output and
  the script exits cleanly.
- The 5h / 7d windows are account-level: the snapshot reflects the freshest
  reading across all your agents *and* your own interactive Claude Code
  sessions, so you don't need a dedicated agent alive just to keep it current.

## Use up an about-to-expire 5h window

Dedicate an agent to this and let one cron job own its whole lifecycle: it starts
the agent during the tail of a 5h window when there's budget to spare and the week
is on pace, then stops it once the window rolls over or the week falls off pace.
Branching on the agent's state each tick needs no `at`/`atd` and no second job --
and letting it run one tick into the fresh window warms that window too.

```bash
#!/usr/bin/env bash
# use-extra.sh -- run a DEDICATED agent during the tail of a 5h window when
# there's budget to spare, and stop it once the window rolls over or the week is
# no longer on pace. One cron job owns the whole start/stop lifecycle (no `at`).
# Point it at an agent set aside for this -- it gets started and stopped
# automatically, so don't aim it at one you're actively driving yourself.
set -euo pipefail

AGENT="my-agent"

snapshot="$(mngr usage --format json)"

# From account-level usage, decide whether the agent should be running now. We
# keep age-stale readings (a quiet account is exactly when there's leftover
# budget), but require the 5h window to still be OPEN (seconds_until_reset > 0):
# once it has reset its cached used%/elapsed% are from the previous window. Emit:
#   START -- worth (re)launching: last 10% of an open 5h window, <80% of it used
#            (budget left to burn), and the week on pace to spare some.
#   KEEP  -- should stay running, but not worth a fresh start (>=80% used).
#   ""    -- shouldn't be running now (window rolled over, or week off pace).
# Pace check: used% < elapsed% * (1 - 0.30 * (100 - elapsed%) / 100), with
# elapsed% = how far into the rolling 7-day cycle we are -- a tapering safety
# margin (widest early, zero by cycle's end) that keeps us clear of your usage.
status="$(jq -r '
  .sources[]
  | select(.source == "claude")
  | select((.five_hour.seconds_until_reset // 0) > 0)
  | select((.five_hour.elapsed_percentage // 0) > 90)
  | (.seven_day.elapsed_percentage // 0) as $week_elapsed
  | select((.seven_day.used_percentage // 100)
           < $week_elapsed * (1 - 0.30 * (100 - $week_elapsed) / 100))
  | if (.five_hour.used_percentage // 100) < 80 then "START" else "KEEP" end
' <<<"$snapshot")"

# Branch on the agent's current lifecycle state.
state="$(mngr list --include "name == \"$AGENT\"" --format json | jq -r '.agents[0].state // "MISSING"')"

case "$state" in
  STOPPED)
    # Launch into the window's tail. Running one tick past the reset warms the
    # fresh window; the next tick then stops us (status goes empty).
    if [[ "$status" == "START" ]]; then
      mngr start "$AGENT" && mngr message "$AGENT" --message "continue where you left off"
    fi
    ;;
  RUNNING | WAITING)
    # Stop once the reason to run is gone: window rolled over (we're early in a
    # fresh one) or the week fell off pace.
    if [[ -z "$status" ]]; then
      mngr stop "$AGENT"
    fi
    ;;
  *)
    : # MISSING / DONE / REPLACED / UNKNOWN -- leave it alone.
    ;;
esac
```

```cron
# cron starts with a bare PATH; set one that finds mngr and jq (adjust to your install)
PATH=/usr/local/bin:/usr/bin:/bin:/home/you/.local/bin
*/10 * * * * /path/to/use-extra.sh
```

## Warm a fresh 5h window early

The 5h window starts when you send your first prompt and runs five hours from
there -- so it pays to start it *before* you actually sit down to work. If a
throwaway prompt opens the window an hour or two ahead, it resets partway
through your session (on average ~2.5h in) instead of a full 5h later, giving
you a fresh quota window sooner. This recipe keeps a window warm automatically:
the moment the last one elapses, it fires a one-off prompt to open the next.

`resets_at < now` means the last recorded 5h window boundary is already past --
a fresh window is open and unclaimed (the past-reset half of `is_stale`). Nudge a
dedicated warming agent then to fire one prompt and open the new window. We reuse
one agent across boundaries (create once, then start/message/stop) and never
destroy it: a *stopped* agent keeps its events, so the snapshot reflects the new
window and the check below won't re-fire until the next window rolls -- no marker
file needed.

```bash
#!/usr/bin/env bash
# warm-window.sh -- open a fresh 5h window as soon as the last one has elapsed.
set -euo pipefail

WARMER="window-warmer"

snapshot="$(mngr usage --format json)"

# Has the last recorded 5h window already reset? (resets_at in the past, compared
# to the snapshot's own `now` -- not is_stale, which would also fire on merely
# age-stale data whose window has NOT yet reset.)
elapsed="$(jq -r '
  .now as $now
  | .sources[]
  | select(.source == "claude")
  | select((.five_hour.resets_at // 0) > 0 and .five_hour.resets_at < $now)
  | "yes"
' <<<"$snapshot")"

[[ "$elapsed" == "yes" ]] || exit 0

# Make sure the warmer is up: create it the first time (pinned to cheap Haiku),
# else (re)start the one we stopped last boundary (`|| true` tolerates it already
# running from an interrupted run).
if mngr list --include "name == \"$WARMER\"" --ids | grep -q .; then
  mngr start "$WARMER" 2>/dev/null || true
else
  mngr create "$WARMER" claude --no-connect -- --model haiku
fi

# One cheap prompt opens the new 5h window. Wait for the turn to finish, then STOP
# (don't destroy) -- the agent and its fresh reading persist for reuse next time.
mngr message "$WARMER" --message 'just say hi'
mngr wait "$WARMER" WAITING --timeout 5m
mngr stop "$WARMER"
```

```cron
# cron starts with a bare PATH; set one that finds mngr, jq, and claude (adjust to your install)
PATH=/usr/local/bin:/usr/bin:/bin:/home/you/.local/bin
*/10 * * * * /path/to/warm-window.sh
```

## Dispatch tasks from a queue directory

Drop one Markdown file per task into a `todo/` directory and let `cron` fan them
out, capped at two in flight.

```bash
#!/usr/bin/env bash
# dispatch-task.sh -- start an agent for the next queued task, capped at 2 in flight.
set -euo pipefail

TODO_DIR="$HOME/agent-tasks/todo"
DOING_DIR="$HOME/agent-tasks/in-progress"
PROJECT_DIR="$HOME/code/my-project"   # all tasks target this repo
MAX_PARALLEL=2

# Retire our own finished agents first: pool members that have gone WAITING (done
# with their turn). The queue=tasks label is what marks them as ours -- agents you
# start yourself don't carry it -- so this never stops your own work. Stopping
# them frees pool slots for the cap below.
for a in $(mngr list --include 'labels.queue == "tasks" && state == "WAITING"' --format '{name}'); do
  mngr stop "$a"
done

# Count pool agents still alive (RUNNING or WAITING; not STOPPED/DONE/etc).
# Capping by the shared `queue=tasks` label -- rather than by name -- lets each
# agent be named after its own task while still sharing one concurrency limit.
alive="$(mngr list \
  --include 'labels.queue == "tasks" && (state == "RUNNING" || state == "WAITING")' \
  --ids | wc -l | tr -d ' ')"
[[ "$alive" -lt "$MAX_PARALLEL" ]] || exit 0

# Grab the oldest queued task, if any.
task_file="$(find "$TODO_DIR" -maxdepth 1 -name '*.md' -type f | sort | head -n1)"
[[ -n "$task_file" ]] || exit 0

# Claim it by moving to in-progress/ before spending anything: an atomic mv on
# the same filesystem means a racing tick can't grab the same task.
mkdir -p "$DOING_DIR"
claimed="$DOING_DIR/$(basename "$task_file")"
mv "$task_file" "$claimed" || exit 0

# Name the agent after the task file, sanitized to a valid agent name (lowercase,
# non-alphanumeric runs collapse to a single dash, no leading/trailing dash).
name="$(basename "$claimed" .md | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
[[ -n "$name" ]] || name="task-$(date +%s)"

# Create (auto-starts) from the project repo, tag with the pool label so the cap
# above can see it, and hand it the task file as its first message. --no-connect
# keeps it non-interactive (cron has no TTY to attach a tmux session to).
mngr create "$name" claude --from ":$PROJECT_DIR" --label queue=tasks \
  --message-file "$claimed" --no-connect
```

```cron
# cron starts with a bare PATH; set one that finds mngr and jq (adjust to your install)
PATH=/usr/local/bin:/usr/bin:/bin:/home/you/.local/bin
*/10 * * * * /path/to/dispatch-task.sh
```

Note: this treats `WAITING` (the agent finished its turn) as "task done" and
stops it, freeing the slot. A task that needs a follow-up nudge would need extra
logic.
