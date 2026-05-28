# Cron automation recipes

Recipes for driving `mngr` from `cron` -- relaunching an agent to use up an
expiring usage window, warming a fresh window, or feeding a task queue. Treat
them as inspiration: copy a script, point it at your agents, tune the
thresholds. They build on `mngr usage --format json` (rolling-window snapshots)
and `mngr list` (agent state), branching in plain shell.

`mngr usage wait` blocks a single process until its predicate matches once --
the right tool for a **one-off**: "when the 5h window frees up, kick off this
batch." For a **recurring** policy -- "every so often, if usage looks like X,
do Y" -- you don't want to sit on a `wait` and manually re-arm it after every
match. Let `cron` own the cadence instead: poll the plain snapshot on a schedule
and branch in the shell. The snapshot is the same per-source shape `wait`
evaluates, so `jq` plays the role `--until`'s CEL did.

A few things hold across the usage-driven recipes below:

- `mngr usage --format json` always prints a `sources` array (empty on a
  no-data tick), so a `jq` predicate that matches nothing yields no output and
  the script exits cleanly.
- The 5h / 7d windows are account-level: the snapshot reflects the freshest
  reading across all your agents *and* your own interactive Claude Code
  sessions, so you don't need a dedicated agent alive just to keep it current.
- `cron` runs with a minimal `PATH`; make sure `mngr`, `jq`, `at`, and `claude`
  resolve (set `PATH` at the top of the crontab, or call them by absolute path).

## Soak up an about-to-expire 5h window

Fire near the end of a 5h window when there's budget left both in that window
and (on pace) in the week, relaunch a known agent, and schedule a stop for the
window boundary so it doesn't bleed into the next window:

```bash
#!/usr/bin/env bash
# soak-window.sh -- relaunch an agent to use up an about-to-expire 5h window.
set -euo pipefail

AGENT="my-agent"

snapshot="$(mngr usage --format json)"

# Scope to the Claude writer's account-level windows, and skip stale readings
# (is_stale also covers a window that already reset -- its cached percentage is
# from the previous window, so acting on it would be wrong). Emit the window's
# seconds-until-reset only when:
#   - >90% of the 5h window has elapsed (we're near its end), AND
#   - <80% of the 5h window is used (budget left to burn before it resets), AND
#   - the 7d window has PACE headroom: remaining budget exceeds 70% of the
#     remaining week, i.e. (100 - used%) > 0.70 * (100 - elapsed%). This is the
#     linear-pace line (used% < elapsed%) loosened by a 30% margin on what's LEFT
#     of the week rather than a fixed margin on used%. So it stays strict early
#     (a flat ceiling would happily spend at 70% on a Monday) yet, as the week
#     ends and remaining time -> 0, the right side -> 0 and it converges to
#     "launch if there's any capacity left at all." Both windows carry
#     window_seconds, so the reader derives elapsed_percentage; raise the 0.70
#     toward 1.0 to shrink the margin (1.0 == strict linear pace, no headroom).
secs="$(jq -r '
  .sources[]
  | select(.source == "claude" and .is_stale == false)
  | select((.five_hour.elapsed_percentage // 0) > 90)
  | select((.five_hour.used_percentage    // 100) < 80)
  | select((100 - (.seven_day.used_percentage // 100)) > 0.70 * (100 - (.seven_day.elapsed_percentage // 0)))
  | .five_hour.seconds_until_reset
' <<<"$snapshot")"

# No source matched the predicate -> nothing to do this tick.
[[ -n "$secs" ]] || exit 0

# Don't nudge an agent that's already up (RUNNING or WAITING): `mngr start` errors
# on a live agent, and we'd just be firing a redundant "continue" into a session
# that's already working. Only a STOPPED agent gets (re)launched here.
if mngr list --include "name == \"$AGENT\" && (state == \"RUNNING\" || state == \"WAITING\")" --ids | grep -q .; then
  exit 0
fi

mngr start "$AGENT" && mngr message "$AGENT" --message "continue where you left off"

# Schedule the stop for the window boundary, floored to the minute (`at`'s
# resolution). Requires a running `at` daemon (`atd`). No atd? A detached timer
# works too and keeps the exact seconds:
#   nohup bash -c "sleep $secs && mngr stop $AGENT" >/dev/null 2>&1 &
echo "mngr stop $AGENT" | at "now + $(( secs / 60 )) minutes"
```

Run it every 10 minutes from `cron`:

```cron
*/10 * * * * /path/to/soak-window.sh
```

## Warm a fresh window once the last one has elapsed

The reader already computes a past-reset signal -- it's the half of `is_stale`
that fires the human "a window already reset" warning. In JSON you reconstruct
it precisely by comparing a window's `resets_at` (a unix timestamp) against the
snapshot's own top-level `now`: `resets_at < now` means the most recently
recorded 5h window boundary is in the past, i.e. a fresh window is open and
unclaimed. Fire one throwaway headless turn then -- `claude -p` runs a single
non-interactive prompt and exits -- to open (and prime the cache of) the new
window without standing up a full agent:

```bash
#!/usr/bin/env bash
# warm-window.sh -- open a fresh 5h window as soon as the last one has elapsed.
set -euo pipefail

snapshot="$(mngr usage --format json)"

# Emit the elapsed window's resets_at (a unix ts) when it lies in the past. We
# compare against the snapshot's own `now` rather than keying off is_stale, which
# would also fire on merely age-stale data whose window has NOT yet reset.
elapsed_at="$(jq -r '
  .now as $now
  | .sources[]
  | select(.source == "claude")
  | select((.five_hour.resets_at // 0) > 0 and .five_hour.resets_at < $now)
  | .five_hour.resets_at
' <<<"$snapshot")"

[[ -n "$elapsed_at" ]] || exit 0

# Warm at most once per boundary. Headless `claude -p` may not refresh the usage
# reading (the statusline writer captures interactive sessions), so without this
# marker the script could re-warm every tick until your next real session lands.
# Keying the marker on the elapsed resets_at makes it a no-op until the *next*
# window elapses with a different boundary.
marker="$HOME/.cache/mngr-warm-window-last-resets-at"
[[ "$(cat "$marker" 2>/dev/null)" == "$elapsed_at" ]] && exit 0
mkdir -p "$(dirname "$marker")"
printf '%s' "$elapsed_at" > "$marker"

# One cheap non-interactive turn: opens the new 5h window and warms the cache.
claude -p 'just say hi' >/dev/null
```

```cron
*/10 * * * * /path/to/warm-window.sh
```

## Dispatch tasks from a queue directory

Drop one Markdown file per task into a `todo/` directory and let `cron` fan them
out to agents, capped at two in flight. Unlike the recipes above (which relaunch
one known agent), this one *creates* a fresh agent per task, named after the
task file. The concurrency cap is enforced **by label, not by name**: every
agent in the pool gets the same `queue=tasks` label, so counting alive
pool members is one `mngr list` filter regardless of what the individual agents
are called.

```bash
#!/usr/bin/env bash
# dispatch-task.sh -- start an agent for the next queued task, capped at 2 in flight.
set -euo pipefail

TODO_DIR="$HOME/agent-tasks/todo"
DOING_DIR="$HOME/agent-tasks/in-progress"
MAX_PARALLEL=2

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

# Create (auto-starts), tag with the pool label so the cap above can see it, and
# hand it the task file as its first message. --no-connect keeps it
# non-interactive (cron has no TTY to attach a tmux session to).
mngr create "$name" claude --label queue=tasks --message-file "$claimed" --no-connect
```

```cron
*/10 * * * * /path/to/dispatch-task.sh
```

Note: a finished agent sits in `WAITING`, which still counts as alive and so
keeps holding a pool slot. To free slots automatically, create with an idle
timeout (e.g. add `--idle-timeout 30m`) so idle agents retire themselves, or
stop them in a separate cleanup step.
