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

## A spare-capacity check

A useful building block is spotting when there's capacity that's likely to go
unused, so a recipe can spend it. Here we call it "spare" when the 5h window still
has budget (<80% used) *and* weekly usage is under pace -- below a line that starts
~30% under the plain `used% = elapsed%` pace early in the rolling 7-day cycle
(elapsed% = how far into the cycle you are) and tapers up to meet it by the cycle's
end. The early margin keeps automation from crowding your own usage.

```bash
#!/usr/bin/env bash
# spare-capacity.sh -- exit 0 if there's spare capacity (as defined above), else
# non-zero (including when there's no usage data to judge from).
set -euo pipefail

mngr usage --format json | jq -e '
  .sources[]
  | select(.source == "claude")
  | (.five_hour.used_percentage // 100)  as $u5
  | (.seven_day.elapsed_percentage // 0) as $elw
  | (.seven_day.used_percentage // 100)  as $uw
  | $u5 < 80 and $uw < $elw * (1 - 0.30 * (100 - $elw) / 100)
' >/dev/null
```

## Use up an about-to-expire 5h window

Set up a dedicated agent with its task first, then let one cron job own its
lifecycle: it starts the agent during the tail of a 5h window when there's budget
to spare and the week is on pace, then stops it once the window rolls over or the
week falls off pace. Letting it run one tick into the fresh window warms that
window too.

```bash
#!/usr/bin/env bash
# use-extra.sh -- run a dedicated agent through the tail of a 5h window, then
# stop it once the window or weekly pace rolls over.
set -euo pipefail

AGENT="my-agent"

snapshot="$(mngr usage --format json)"

# From account-level usage, classify what the agent should be doing. We keep
# age-stale readings (a quiet account is when there's leftover budget) but treat
# an already-reset 5h window (seconds_until_reset <= 0) as a reason to stop -- its
# cached numbers are from the previous window. Emit one of:
#   START -- (re)launch: in the tail of an open 5h window (>90% elapsed) with
#            spare capacity (5h budget left, weekly under the pace line above).
#   STOP  -- shut down: the 5h window left its tail / rolled over, OR weekly usage
#            passes the strict pace line, used% > elapsed%.
#   KEEP  -- hold the current state: still in the tail and under the strict weekly
#            line, but not eligible to (re)launch -- weekly is in the hysteresis band,
#            or the 5h budget is already spent.
#   ""    -- no Claude usage data this tick: do nothing.
# The two lines differ on purpose: the gap is hysteresis, so a running agent isn't
# stopped the moment it nudges past the START margin.
status="$(jq -r '
  .sources[]
  | select(.source == "claude")
  | (.five_hour.seconds_until_reset // 0) as $open
  | (.five_hour.elapsed_percentage // 0)  as $el5
  | (.five_hour.used_percentage // 100)   as $u5
  | (.seven_day.elapsed_percentage // 0)  as $elw
  | (.seven_day.used_percentage // 100)   as $uw
  | if   ($open <= 0 or $el5 <= 90 or $uw > $elw)                    then "STOP"
    elif ($u5 < 80 and $uw < $elw * (1 - 0.30 * (100 - $elw) / 100)) then "START"
    else                                                                 "KEEP"
    end
' <<<"$snapshot")"

# Branch on the agent's current lifecycle state.
state="$(mngr list --include "name == \"$AGENT\"" --format json | jq -r '.agents[0].state // "MISSING"')"

case "$state" in
  STOPPED)
    # Launch into the window's tail; the next tick's STOP shuts us down once it rolls.
    if [[ "$status" == "START" ]]; then
      mngr start "$AGENT" && mngr message "$AGENT" --message "continue where you left off"
    fi
    ;;
  RUNNING | WAITING)
    # Stop only on an explicit STOP (window left its tail, or weekly pace caught
    # up). KEEP -- or empty, i.e. no data this tick -- leaves it running.
    if [[ "$status" == "STOP" ]]; then
      mngr stop "$AGENT"
    fi
    ;;
  *)
    : # MISSING / DONE / REPLACED / UNKNOWN -- leave it alone.
    ;;
esac
```

## Warm a fresh 5h window early

The 5h window starts when you send your first prompt and runs five hours from
there -- so it pays to start it *before* you actually sit down to work. If a
throwaway prompt opens the window an hour or two ahead, it resets partway
through your session (on average ~2.5h in) instead of a full 5h later, giving
you a fresh quota window sooner. This recipe keeps a window warm automatically:
the moment the last one elapses, it fires a one-off prompt to open the next.

```bash
#!/usr/bin/env bash
# warm-window.sh -- open a fresh 5h window as soon as the last one has elapsed.
set -euo pipefail

WARMER="window-warmer"
PROJECT_DIR="$HOME/code/my-project"   # any git repo already trusted in Claude Code

# The warmer does no real work, so its repo is irrelevant -- any trusted one
# works. cron starts in $HOME (not a repo), so cd in to give `mngr create` a git
# root.
cd "$PROJECT_DIR"

snapshot="$(mngr usage --format json)"

# Fire only when the last recorded 5h window has already reset -- resets_at in the
# past (vs the snapshot's own `now`) means a fresh window is open and unclaimed.
elapsed="$(jq -r '
  .now as $now
  | .sources[]
  | select(.source == "claude")
  | select((.five_hour.resets_at // 0) > 0 and .five_hour.resets_at < $now)
  | "yes"
' <<<"$snapshot")"

[[ "$elapsed" == "yes" ]] || exit 0

# Clean up any warmer left over from an interrupted run, then spin up a fresh one.
mngr destroy "$WARMER" --force 2>/dev/null || true
mngr create "$WARMER" claude --no-connect -- --model haiku

# One cheap prompt opens the new 5h window. Wait for the turn to finish, then
# destroy the warmer -- it's a throwaway, and its usage events are preserved on
# destroy (the `preserve_on_destroy` usage-plugin option, on by default).
mngr message "$WARMER" --message 'just say hi'
mngr wait "$WARMER" WAITING --timeout 5m
mngr destroy "$WARMER" --force
```

## Dispatch tasks from a queue directory

Drop one Markdown file per task into a `todo/` directory and let `cron` fan them
out -- capped at two in flight, and only while there's spare capacity to spend.

```bash
#!/usr/bin/env bash
# dispatch-task.sh -- start an agent for the next queued task, capped at 2 in flight.
set -euo pipefail

TODO_DIR="$HOME/agent-tasks/todo"
DOING_DIR="$HOME/agent-tasks/in-progress"
PROJECT_DIR="$HOME/code/my-project"   # all tasks target this repo
MAX_PARALLEL=2

# cron starts in $HOME; cd into the project so agents are created from its git
# root and mngr loads the project's config (create_templates, labels, etc.).
# (Absolute paths below -- $0, TODO_DIR, DOING_DIR -- are unaffected by the cd.)
cd "$PROJECT_DIR"

# Retire finished agents first: pool members (queue=live) that have gone WAITING,
# i.e. done with their turn. Stop each and move it to queue=in-review -- that frees
# a pool slot (the cap below counts only queue=live) while parking the agent for
# you to restart and inspect later (`mngr list --label queue=in-review`). The cron
# only manages queue=live, so it never touches an in-review agent again.
for a in $(mngr list --include 'labels.queue == "live" && state == "WAITING"' --format '{name}'); do
  mngr stop "$a" && mngr label "$a" --label queue=in-review
done

# After the retirement above, any live pool agents left are RUNNING; count those
# and bail if we're at the cap.
alive="$(mngr list --include 'labels.queue == "live" && state == "RUNNING"' --ids | wc -l | tr -d ' ')"
[[ "$alive" -lt "$MAX_PARALLEL" ]] || exit 0

# Only launch if there's spare capacity going unused.
"$(dirname "$0")/spare-capacity.sh" || exit 0

# Grab the oldest queued task, if any. (For a random order, use `sort -R`.)
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

# Create the agent, tag it into the live pool, and hand it the task file as its
# first message. --no-connect keeps it non-interactive (cron has no TTY to attach
# a tmux session to). --from ":$PROJECT_DIR" sources from the project repo, and
# --branch main: gives each agent its own fresh branch off main (empty NEW ->
# mngr/<name>) so concurrent tasks never share a working branch.
mngr create "$name" claude --from ":$PROJECT_DIR" --branch main: --label queue=live \
  --message-file "$claimed" --no-connect
```

Finished agents are stopped and moved to `queue=in-review`; to see them, run
`mngr list --label queue=in-review`.

## Scheduling

`cron` runs with a bare `PATH`, so set one that finds `mngr`, `jq`, `git`,
`tmux`, and `claude`. `mngr`, `claude`, and `uv` install under `~/.local/bin`;
`jq`, `git`, and `tmux` come from your system package manager (`/usr/bin` via
`apt` on Linux, `/opt/homebrew/bin` via Homebrew on Apple Silicon macOS).

```cron
PATH=/usr/bin:/bin:/home/you/.local/bin

*/5 * * * * /path/to/your/script.sh
```

### macOS: run via a LaunchAgent (Keychain-aware)

On macOS, `cron` runs outside your GUI (Aqua) login session, so it can't reach
the login Keychain where Claude Code stores its credentials -- cron-launched
agents come up "Not logged in" and do nothing. A user **LaunchAgent** runs
inside that session, so its agents authenticate with your normal Claude login.

Put a plist in `~/Library/LaunchAgents/`; it runs your `.sh` directly (the
script needs a shebang and `chmod +x`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>                <string>com.you.mngr-dispatch</string>
  <key>ProgramArguments</key>     <array><string>/path/to/your/script.sh</string></array>
  <key>EnvironmentVariables</key> <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/you/.local/bin</string></dict>
  <key>StartInterval</key>        <integer>300</integer>
  <key>RunAtLoad</key>            <true/>
  <key>StandardOutPath</key>      <string>/Users/you/Library/Logs/mngr-dispatch.log</string>
  <key>StandardErrorPath</key>    <string>/Users/you/Library/Logs/mngr-dispatch.log</string>
</dict>
</plist>
```

- `StartInterval` (seconds) sets the run cadence -- `300` is cron's `*/5`.
- `EnvironmentVariables` -> `PATH` needs the same entries as cron above: the
  Homebrew prefix (`/opt/homebrew/bin`) and `~/.local/bin`.
- `StandardOutPath` / `StandardErrorPath` capture output to a log file.

Load it into your session (and start it running):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.mngr-dispatch.plist
```

Unload it (by label):

```bash
launchctl bootout gui/$(id -u)/com.you.mngr-dispatch
```

Unlike cron, a LaunchAgent only runs while you're logged in.
