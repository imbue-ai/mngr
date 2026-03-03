---
name: asciinema-demos
description: Create 1-3 short asciinema demo recordings (5-20 seconds each) that demonstrate completed work. Use only when explicitly instructed to create demos.
---

# Creating Asciinema Demos

This skill provides guidelines for creating short, looping terminal demo recordings that visually demonstrate completed work. The demos serve as proof that a task was done correctly and help reviewers quickly understand what changed.

## Overview

Each demo session produces 1-3 recordings (each 5-20 seconds) as looping GIFs. The workflow is:

1. Plan what to demo
2. Write demo and cleanup scripts
3. Record with asciinema
4. Verify the recordings
5. Iterate if needed
6. Convert to GIF and (optionally) upload to a PR

## Prerequisites

The following tools must be available:

- `asciinema` (terminal recorder) -- records .cast files
- `agg` (asciinema gif generator) -- converts .cast to .gif
- `python3` with `json` module (standard library) -- for .cast file verification

If they are not, simply make a note in the PR that the demos could not be generated, and which tools should be installed to enable them.

## Step 1: Plan the Demos

Before recording anything, think carefully about what to demo. This is the most important step.

**Ask yourself:**

- What are the 1-3 most important things to show?
- What would convince a reviewer that the task was completed correctly?
- What is the shortest path to showing each thing?
- How can I ensure that all resulting demo resources are always safely cleaned up by the demo script after each execution?

**Common demo types:**

| Scenario | What to show |
|---|---|
| New CLI command | Run the command with typical arguments, show the output |
| Bug fix | Show the fixed behavior |
| Data/config change | Show the data exists and has correct values (e.g., `cat`, `jq`, `grep`) and that the core dependent behavior works |
| Performance improvement | Run a benchmark or timed command showing the improvement (and the necessary context to understand) |
| New feature in existing command | Run the command exercising the new feature |
| Refactor (no behavior change) | Show that the core dependent behavior(s) still work |

**Guidelines:**

- Each demo should be 5-20 seconds. Shorter is better.
- Focus on the output that matters -- don't show lengthy setup or irrelevant output.
- If demonstrating data, use commands like `cat`, `head`, `jq`, `grep`, or `sqlite3` to show the relevant parts.
- If the change is not directly visible in the CLI (e.g., internal refactor), demo the tests passing or show downstream effects.

## Step 2: Write Demo and Cleanup Scripts

For each demo, create a bash script for that specific demo. Place them in a temporary location or in `.demos/scripts/`.

**Template for a demo script:**

```bash
#!/usr/bin/env bash
# Demo: Brief description of what this demonstrates

# Optionally set up a clean prompt for the recording
export PS1='$ '

# Add brief pauses between commands so the viewer can read the output
echo "$ some-command --flag"
some-command --flag
sleep 1

echo ""
echo "$ another-command"
another-command
sleep 1
```

**Important considerations for demo scripts:**

### Handling commands that block or require input

For commands that would normally block waiting for input or run indefinitely:

- Use `timeout` to limit execution time: `timeout 5 some-long-command`
- Pipe input for interactive commands: `echo "y" | some-command`
- Use `yes | head -1 |` for yes/no prompts
- For commands that start background processes, run them and immediately show the result:
  ```bash
  some-command --background
  sleep 2
  show-status-command
  ```

### Simulating typed commands

To make the demo look natural (as if someone is typing), you can echo the command before running it:

```bash
# Show the command being "typed", then run it
echo '$ mng list'
mng list
sleep 1
```

Or, for a more polished look, use a helper function that simulates typing:

```bash
type_cmd() {
    local cmd="$1"
    printf '$ '
    for ((i=0; i<${#cmd}; i++)); do
        printf '%s' "${cmd:$i:1}"
        sleep 0.03
    done
    printf '\n'
    eval "$cmd"
}

type_cmd "mng list"
sleep 1
```

### Keeping output clean

- Set `export PS1='$ '` for a clean prompt
- Redirect stderr if it would clutter the output: `command 2>/dev/null`
- Use `head -n 20` or similar to truncate long output
- Clear the screen between demos if needed: `clear`

**Important considerations for cleanup scripts:**

- The cleanup script *MUST* be run after running the demo
- Be *very* careful with `rm` or any destructive commands! Always double-check that the script cannot accidentally delete important files or directories.
- Name cleanup scripts like this: .demos/scripts/demo1_cleanup.sh

## Step 3: Record

Use the helper script `scripts/record_demo.sh` to record each demo:

```bash
./scripts/record_demo.sh <demo_script> <output_name> [options]
```

**Examples:**

```bash
# Basic recording
./scripts/record_demo.sh .demos/scripts/demo1.sh feature-demo; .demos/scripts/demo1_cleanup.sh

# Custom terminal size and speed
./scripts/record_demo.sh .demos/scripts/demo1.sh feature-demo --cols 120 --rows 24 --speed 1.5; .demos/scripts/demo1_cleanup.sh

# Recording without GIF conversion (for faster iteration)
./scripts/record_demo.sh .demos/scripts/demo1.sh feature-demo --no-gif; .demos/scripts/demo1_cleanup.sh
```

The script produces three files in `.demos/` (or the directory specified by `--out-dir`):

- `<name>.cast` -- the asciinema recording
- `<name>.gif` -- the GIF (unless `--no-gif`)
- `<name>.txt` -- plain text dump of the recording output

**Key options:**

| Option | Default | Description |
|---|---|---|
| `--cols N` | 100 | Terminal width |
| `--rows N` | 30 | Terminal height |
| `--theme THEME` | monokai | GIF color theme |
| `--font-size N` | 16 | Font size in pixels for GIF |
| `--speed N` | 1 | Playback speed multiplier |
| `--idle-limit N` | 2 | Max idle time between events (seconds) |
| `--last-frame N` | 3 | How long the final frame displays (seconds) |
| `--out-dir DIR` | .demos | Output directory |
| `--no-gif` | | Skip GIF conversion (faster for iteration) |
| `--no-loop` | | Disable GIF looping |

## Step 4: Verify

This is critical. Since you cannot watch the GIF, you must verify the recording by reading the text dump.

**Read the `.txt` file** to confirm:

1. The expected commands appear in the output
2. The expected output/results are visible
3. There are no error messages or unexpected output
4. The flow makes sense (commands appear in the right order)

```bash
cat .demos/feature-demo.txt
```

You can also inspect the `.cast` file directly to check timing:

```bash
# Check the total duration (time of last event)
tail -1 .demos/feature-demo.cast

# Check all events
cat .demos/feature-demo.cast
```

**Verification checklist:**

- [ ] All expected commands are present in the output
- [ ] All expected results/data are visible
- [ ] No error messages or tracebacks
- [ ] Recording duration is 5-20 seconds (check the timestamp of the last event in the .cast file)
- [ ] Terminal size is appropriate for the content (no wrapping/truncation issues)

## Step 5: Iterate

If the recording does not look right:

1. Identify the problem from the `.txt` dump
2. Fix the demo script
3. Re-record (use `--no-gif` while iterating for speed)
4. Verify again
5. Once satisfied, do a final recording with GIF conversion

Common problems and fixes:

| Problem | Fix |
|---|---|
| Output is truncated/wrapped | Increase `--cols` |
| Recording is too long | Remove unnecessary `sleep` calls, use `--speed 2` |
| Recording is too short | Add `sleep` calls between commands |
| Command produced an error | Fix the demo script or the underlying issue |
| Too much output | Use `head`, `tail`, or `grep` to filter |
| Interactive command blocked | Use `timeout`, pipe input, or mock the interaction |

## Step 6: Upload to PR

If the demos should be attached to a GitHub PR, you need to make the GIF accessible via URL.

**NEVER try to commit files the demo files, especially not the GIFs**.

**Instead, upload via GitHub's attachment API**

GitHub allows uploading images by posting them as assets. This avoids committing binary files to the repo. Use `gh` to create a release asset or attach to an issue/PR comment via the API.

## Tips

- **Start with `--no-gif`** while iterating on the demo script. Only convert to GIF once you are satisfied with the `.txt` output.
- **Keep demos focused.** One concept per demo. If you need to show multiple things, make multiple short demos rather than one long one.
- **Use `--speed 1.5` or `--speed 2`** if the demo has natural pauses that would make it feel slow.
- **For data verification demos**, use colorized output when possible (e.g., `jq` with colors, `grep --color`). This makes the GIF more readable.
- **Always run the cleanup script** after recording to ensure no leftover state could affect future runs or the user's environment.
- **Output directory**: The default `.demos/` directory is gitignored. **NEVER commit GIFs!**
