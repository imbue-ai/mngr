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
PROJECT_DIR="$HOME/code/my-project"   # any git repo; the warmer just needs a home

# cron starts in $HOME (usually not a git repo); cd into the project so `mngr
# create` has a git root to branch from and picks up the project's config.
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

# Make sure the warmer is up: create it the first time, else (re)start the one we
# stopped last boundary (`|| true` tolerates it already running from an
# interrupted run).
if mngr list --include "name == \"$WARMER\"" --ids | grep -q .; then
  mngr start "$WARMER" 2>/dev/null || true
else
  mngr create "$WARMER" claude --no-connect -- --model haiku
fi

# One cheap prompt opens the new 5h window. Wait for the turn to finish, then STOP
# (don't destroy): a stopped agent keeps its events, so the snapshot reflects the
# new window and the check above won't re-fire until the next window rolls.
mngr message "$WARMER" --message 'just say hi'
mngr wait "$WARMER" WAITING --timeout 5m
mngr stop "$WARMER"
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

# Create (auto-starts) in the project repo we cd'd into, tag it into the live
# pool, and hand it the task file as its first message. --no-connect keeps it
# non-interactive (cron has no TTY to attach a tmux session to).
mngr create "$name" claude --label queue=live \
  --message-file "$claimed" --no-connect
```

Finished agents are stopped and moved to `queue=in-review`; to see them, run
`mngr list --label queue=in-review`.

## Scheduling

`cron` runs with a bare environment, so each script has to establish its own
context. Two things bite in practice:

- **`PATH`** is minimal, so set one that finds `mngr`, `jq`, `git`, `tmux`, and
  `claude`. `mngr` (and the Claude CLI) install under `~/.local/bin`; `jq`,
  `git`, and `tmux` come from your system package manager.
- **The working directory** is your home dir, which usually isn't a git repo --
  that's why the recipes above `cd "$PROJECT_DIR"` before spawning an agent.
  `mngr` resolves project-scoped settings (`.mngr/` config, `create_templates`,
  labels) from the cwd's git worktree root, so running inside the project is
  what makes those settings apply. (`--from` only sets the agent's source repo,
  not which config loads.)

On Linux (deps installed via `apt`, so they land in `/usr/bin`):

```cron
PATH=/usr/bin:/bin:/home/you/.local/bin

*/5 * * * * /path/to/your/script.sh
```

On macOS (Homebrew on Apple Silicon puts deps in `/opt/homebrew/bin`; Intel Macs
use `/usr/local/bin`, kept below for either):

```cron
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/you/.local/bin

*/5 * * * * /path/to/your/script.sh
```

On macOS, though, prefer a **LaunchAgent** over `cron` for anything that drives
a `claude` agent -- see the next section for why.

### macOS: run via a LaunchAgent (Keychain-aware)

`cron` jobs on macOS run outside your GUI (Aqua) login session, so they can't
reach the login Keychain -- and that's where Claude Code keeps its credentials
(Keychain service `Claude Code-credentials`), not in a file. A cron-launched
agent therefore comes up unauthenticated: its banner reads
`API Usage Billing - Not logged in - Run /login`, and it does nothing.

A user **LaunchAgent** loaded into your Aqua session *does* have Keychain
access, so agents authenticate with your normal Claude login (the banner reads
e.g. `Claude Max` instead of `Not logged in`) -- no API key on disk. It's the
macOS-native answer to "my scheduled mngr agents come up not-logged-in".

Drop a plist in `~/Library/LaunchAgents/`. It runs your `.sh` directly (give the
script a shebang and `chmod +x` it); no wrapper needed:

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

- `StartInterval` (seconds) is the periodic-run knob -- `300` is the equivalent
  of cron's `*/5`.
- `EnvironmentVariables` -> `PATH` carries the same caveat as cron: a bare
  launchd `PATH` won't find your tools, so include the Homebrew prefix
  (`/opt/homebrew/bin`, for `tmux` and `node`) and `~/.local/bin` (`mngr`,
  `claude`, `uv`).
- `StandardOutPath` / `StandardErrorPath` capture output to a log file.
  LaunchAgents don't mail their output, so this also avoids cron's "You have new
  mail" noise.
- The cwd caveat still applies: the job runs outside your repo, so the script
  must `cd` into it (or set `MNGR_PROJECT_CONFIG_DIR`) for mngr to pick up the
  project's `.mngr/settings.toml`.

Load it into your session (and start it running):

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.mngr-dispatch.plist
```

Unload it (by label):

```bash
launchctl bootout gui/$(id -u)/com.you.mngr-dispatch
```

Tradeoff vs cron: a LaunchAgent only runs while you're logged in, whereas cron
runs regardless. On a dev machine that's usually fine -- often what you want.
