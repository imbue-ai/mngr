# mngr-schedule

Run AI agents on a schedule.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr schedule` command for scheduling recurring invocations of `mngr` commands (even on remote providers)

## Overview

`mngr schedule` lets you set up cron-scheduled triggers that automatically run `mngr` commands (create, start, message, exec) at regular intervals -- for example, a nightly code review agent or a periodic test runner.

## Usage

```bash
# Add a nightly agent that runs at 2am in modal
mngr schedule add --command create --args "--type claude --message 'review recent PRs' --provider modal" --schedule "0 2 * * *" --provider modal

# Pin the timezone the cron is interpreted in (modal only). Without --timezone,
# the schedule uses the deploying machine's local timezone, so the fire time
# depends on where you deployed from.
mngr schedule add --command create --args "..." --schedule "0 0 * * *" --timezone America/Los_Angeles --provider modal

# Add a named trigger that runs locally
mngr schedule add nightly-test-checker --command create --args "--message 'make sure all tests are passing'" --schedule "0 3 * * *" --provider local

# List all active local schedules
mngr schedule list --provider local

# List all modal schedules including disabled ones
mngr schedule list --provider modal --all

# Update an existing trigger
mngr schedule update my-trigger --schedule "0 4 * * *"

# Disable a trigger without removing it
mngr schedule update my-trigger --disabled

# Test a trigger by running it immediately
mngr schedule run my-trigger

# Remove a trigger
mngr schedule remove my-trigger

# Remove multiple triggers without confirmation
mngr schedule remove trigger-1 trigger-2 --force
```

## Subcommands

Run `mngr schedule <subcommand> --help` for more details on each subcommand:

- **`add`** -- Create a new scheduled trigger
- **`remove`** -- Remove one or more scheduled triggers
- **`update`** -- Modify fields of an existing trigger
- **`list`** -- List scheduled triggers
- **`run`** -- Execute a trigger immediately for testing

## Packaging code for remote execution

Running `mngr` commands in a scheduled environment like Modal requires four things to be available in the Modal Function execution environment:

1. The `mngr` CLI itself.
2. For the `create` command: the target project code the agent will run (or it must be supplied via the command, e.g. `--snapshot <snapshot-id>`).
3. The environment variables and files the command refers to.
4. The `mngr` configuration.

The `mngr schedule` plugin handles all of these automatically. The sections below describe how.

### 1. `mngr` CLI availability

The function's base image is built from the `mngr` Dockerfile, which already includes `mngr` and all its dependencies. Your project is then layered on top: it is packaged and extracted into the container at a configurable path (default `/code/project`, set by `--target-dir`), which becomes the working directory.

### 2. Code availability for `create` commands

There are two modes for how the target repo is packaged:

1. **incremental** (default): the current HEAD commit hash is resolved and reused across deploys from the same repo, so the project doesn't need to be repackaged and uploaded each time.
2. **full**: the entire current HEAD state of the repo (or the whole folder, if not a git repo) is packaged and uploaded on each deploy. Pass `--full-copy` to enable.

#### Auto-merge at runtime

For a git repo, the scheduled function fetches and merges the latest code from the deployed branch before each run, so the agent always works with up-to-date code.

This requires `GH_TOKEN` to be available in the deployed environment (via `--pass-env` or `--env-file`).

Use `--no-auto-merge` to disable this behavior, or `--auto-merge-branch <branch>` to merge from a specific branch (defaults to the current branch at deploy time).

### 3. Ensuring environment variable and file availability for remote execution

The `mngr schedule` plugin automatically forwards any secrets and files required by the scheduled create or start commands. The "message" and "exec" commands need no files or environment variables.

### 4. Ensuring `mngr` configuration availability for remote execution

The `mngr schedule` plugin automatically syncs the relevant `mngr` configuration into the execution environment. This includes much of the data in `~/.mngr/` (except your own personal SSH keys, which are never transferred).

So that you can connect to the newly created agent, `mngr schedule add` automatically includes your SSH key as a known host for "create" and "start" commands.
