# Cron automation recipes

Ideas for **recurring** usage-driven automation: let `cron` poll
`mngr usage --format json` on a schedule and act when usage looks a certain way.
For a **one-off** ("when the 5h window frees up, kick off this batch") reach for
`mngr usage wait` instead -- see [Waiting on a predicate](../README.md#waiting-on-a-predicate).

Across the usage-driven recipes below:

- `mngr usage --format json` always prints a `sources` array (empty on a
  no-data tick), so a `jq` predicate that matches nothing yields no output and
  the script exits cleanly.
- The 5h / 7d windows are account-level: the snapshot reflects the freshest
  reading across all your agents *and* your own interactive Claude Code
  sessions, so you don't need a dedicated agent alive just to keep it current.

## Use up an about-to-expire 5h window

Fire near the end of a 5h window when there's budget left both in that window
and (on pace) in the week, relaunch a known agent, and schedule a stop for the
window boundary so it doesn't bleed into the next window:

```bash
#!/usr/bin/env bash
# use-extra.sh -- relaunch an agent to use up an about-to-expire 5h window.
set -euo pipefail

AGENT="my-agent"

snapshot="$(mngr usage --format json)"

# Scope to the Claude writer's account-level windows, and skip stale readings
# (is_stale also covers a window that already reset -- its cached percentage is
# from the previous window, so acting on it would be wrong). Emit the window's
# seconds-until-reset only when:
#   - >90% of the 5h window has elapsed (we're near its end), AND
#   - <80% of the 5h window is used (budget left to burn before it resets), AND
#   - the week's budget looks on track to go partly unused -- a "pace check": are
#     we spending it slower than the week is elapsing? If so there's headroom to
#     spare, so filling the expiring 5h window costs us nothing we'd miss. A
#     safety margin (widest early in the cycle, tapering to zero by its end) keeps
#     us clear of your own usage. With elapsed% = how far into the rolling 7-day
#     cycle we are:
#     used% < elapsed% * (1 - 0.30 * (100 - elapsed%) / 100)
secs="$(jq -r '
  .sources[]
  | select(.source == "claude" and .is_stale == false)
  | select((.five_hour.elapsed_percentage // 0) > 90)
  | select((.five_hour.used_percentage    // 100) < 80)
  | (.seven_day.elapsed_percentage // 0) as $week_elapsed
  | select((.seven_day.used_percentage // 100) < $week_elapsed * (1 - 0.30 * (100 - $week_elapsed) / 100))
  | .five_hour.seconds_until_reset
' <<<"$snapshot")"

# Nothing matched the predicate this tick -> nothing to do.
[[ -n "$secs" ]] || exit 0

# Only (re)launch a STOPPED agent. If it's RUNNING/WAITING it's already working;
# if it's DONE or in any other state, leave it be rather than assume a relaunch
# is the right move.
mngr list --include "name == \"$AGENT\" && state == \"STOPPED\"" --ids | grep -q . || exit 0

mngr start "$AGENT" && mngr message "$AGENT" --message "continue where you left off"

# Schedule the stop just past the window boundary, rounding UP to the minute
# (`at`'s resolution) so the agent's first request lands in the fresh window and
# warms it. Requires a running `at` daemon (`atd`); without one, a detached timer
# with a little grace works too:
#   nohup bash -c "sleep $((secs + 30)) && mngr stop $AGENT" >/dev/null 2>&1 &
echo "mngr stop $AGENT" | at "now + $(( (secs + 59) / 60 )) minutes"
```

```cron
# cron starts with a bare PATH; set one that finds mngr, jq, and at (adjust to your install)
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
a fresh window is open and unclaimed (the past-reset half of `is_stale`). Fire a
throwaway headless turn then -- `claude -p` runs one non-interactive prompt and
exits -- to open the new window without standing up a full agent:

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

# One cheap non-interactive turn is enough to start the next 5h window.
claude -p 'just say hi' --model haiku >/dev/null
```

```cron
# cron starts with a bare PATH; set one that finds mngr, jq, and claude (adjust to your install)
PATH=/usr/local/bin:/usr/bin:/bin:/home/you/.local/bin
*/10 * * * * /path/to/warm-window.sh
```

## Dispatch tasks from a queue directory

Drop one Markdown file per task into a `todo/` directory and let `cron` fan them
out, capped at two in flight. Unlike the recipes above, this *creates* a fresh
agent per task, named after the task file. The concurrency cap is **by label,
not by name**: every pool agent shares the `queue=tasks` label, so counting live
members is one `mngr list` filter no matter what the agents are called.

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
# cron starts with a bare PATH; set one that finds mngr and jq (adjust to your install)
PATH=/usr/local/bin:/usr/bin:/bin:/home/you/.local/bin
*/10 * * * * /path/to/dispatch-task.sh
```

Note: a finished agent sits in `WAITING`, which still counts as alive and so
keeps holding a pool slot. To free slots automatically, create with an idle
timeout (e.g. add `--idle-timeout 30m`) so idle agents retire themselves, or
stop them in a separate cleanup step.
