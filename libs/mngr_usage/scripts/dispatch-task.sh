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
