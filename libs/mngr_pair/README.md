# imbue-mngr-pair

Continuous file synchronization between an agent and your local directory.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr pair` command. Launch with `mngr pair <agent>`.

## Overview

`mngr pair` watches for file changes on both sides and syncs them in real-time using [unison](https://github.com/bcpierce00/unison). If both directories are git repositories, the command first synchronizes git state (branches and commits) before starting continuous file sync.

This is useful for iterative workflows where you want to edit alongside an agent, reviewing and modifying its work as it happens.

## Requirements

- `unison` (file synchronization tool)
  - macOS: `brew install unison` and `brew install autozimu/formulas/unison-fsmonitor`
  - Linux: `sudo apt-get install unison` (inotify provides file watching)

## Usage

```bash
# Basic pairing with an agent
mngr pair my-agent

# Pair to a specific local directory
mngr pair my-agent --target ./local-dir

# One-way sync (agent to local only)
mngr pair my-agent --sync-direction=forward

# One-way sync (local to agent only)
mngr pair my-agent --sync-direction=reverse

# Prefer source files on conflicts
mngr pair my-agent --conflict=source

# Filter to specific files
mngr pair my-agent --include "*.py" --exclude "__pycache__/*"

# Pair a subdirectory of the agent
mngr pair my-agent:/subdir --target ./local-dir

# Skip the git requirement
mngr pair my-agent --no-require-git
```

## Options

### Sync behavior

- `--sync-direction MODE` -- `both` (bidirectional, default), `forward` (agent to local), `reverse` (local to agent)
- `--conflict MODE` -- Conflict resolution for bidirectional sync: `newer` (most recent mtime, default), `source`, `target`
- `--include PATTERN` / `--exclude PATTERN` -- Glob patterns for selective sync (repeatable). `.git` is always excluded.

### Git handling

- `--require-git` / `--no-require-git` -- Require both sides to be git repos (default: enabled)
- `--uncommitted-changes MODE` -- How to handle uncommitted changes during initial git sync: `stash`, `clobber`, `merge`, `fail` (default)

Press Ctrl+C to stop the sync.

## Limitations

- Only local agents are supported (remote agents not yet implemented)
- Clock skew between machines can affect the `newer` conflict mode
