# imbue-mngr-pair

Continuous file synchronization between an agent and your local directory.

A plugin for [mngr](https://github.com/imbue-ai/mngr) that adds the `mngr pair` command. Launch with `mngr pair <agent>`.

## Overview

`mngr pair` watches for file changes on both sides and syncs them in real-time using [unison](https://github.com/bcpierce00/unison). If both directories are git repositories, it first synchronizes git state (branches and commits) before starting continuous file sync. This lets you edit alongside an agent, reviewing and modifying its work as it happens.

## Requirements

- `unison` (file synchronization tool), version 2.52 or newer, plus its
  `unison-fsmonitor` helper -- unison watches for changes through that helper, so
  continuous sync needs both binaries.
  - macOS: `brew install unison` and `brew install autozimu/formulas/unison-fsmonitor`
  - Linux: take both from a [unison release](https://github.com/bcpierce00/unison/releases).
    `apt-get install unison` is not enough: the Debian and Ubuntu package contains no
    `unison-fsmonitor` (and no separate package provides one), and Ubuntu 22.04's unison
    is 2.51, which is too old regardless -- versions below 2.52 cannot interoperate with
    newer ones at all.

Pairing with an agent on a remote host needs the same two binaries *on that host*,
because unison is a client/server protocol rather than a one-shot copy: a second unison
runs on the host and watches its own side. mngr uses what is already installed there if
it is usable, and otherwise installs a pinned static build into `~/.mngr/bin` on the
host. That build only exists for Linux x86_64 (upstream has never published a Linux
arm64 binary), so on any other platform you have to install unison and
`unison-fsmonitor` yourself.

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

# Pair with an agent running on a remote host
mngr pair my-agent@my-vps

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

- Remote hosts need unison 2.52+ *and* `unison-fsmonitor`; mngr can only install those
  for Linux x86_64, so elsewhere (notably Linux arm64, for which upstream publishes no
  binary at all) you must install them yourself. A host carrying only the distro unison
  counts as not having them.
- Clock skew between machines can affect the `newer` conflict mode -- which matters
  more when pairing with a remote agent, since the two clocks are genuinely separate.
