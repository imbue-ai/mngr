# Architecture

## Overview

`mngr` provides a CLI for managing AI [agents](./concepts/agents.md). Multiple agents can run on a single [host](./concepts/hosts.md).

[Hosts](./concepts/hosts.md) are created by [providers](./concepts/providers.md).

Different [agent types](./concepts/agent_types.md) (Claude, Codex, etc.) and [provider backends](./concepts/provider_backends.md) can be defined via configuration or by [plugins](./concepts/plugins.md).

## Agent-centric state model

Agents fully contain their own state on their host.

`mngr` itself has no persistent processes and stores almost no persistent state. Instead, everything is reconstructed from:

1. Queries to **providers** (which inspect Docker labels, Modal tags, local state files, etc.)
2. Queries to **hosts** (to answer "Is SSH responding?" and "Is the process alive?" and read state from the agent filesystem to understand the state of remote **agents**)
3. Configuration files (settings, enabled plugins, etc.)

This means no database, no state corruption, and multiple `mngr` instances can manage the same agents.

Some interactions are gated via a cooperative host lock to avoid race conditions. State-changing operations (e.g. `create`, `start`, `gc`) hold a real `flock(2)` on the host's `host_lock` file -- directly on local hosts, and over a long-lived SSH exec channel on remote hosts -- so that a holder running locally inside the host and a holder running remotely over SSH mutually exclude. The in-host idle-shutdown watcher tests that same lock (a non-blocking `flock` probe), so holding it also suppresses idle shutdown. See [locking spec](../future_specs/locking.md) for the broader (partly future) design.

## Conventions

`mngr` relies on conventions to identify managed resources.

Prefixing a host, tmux session, or Docker container with `mngr-` is enough for `mngr` to recognize and manage it. This prefix can be customized via `MNGR_PREFIX`.

See the [conventions doc](./conventions.md) for full details.

## Responsibilities

mngr is responsible for:
- implementing the [core CLI commands](../README.md) (create, connect, stop, list, push, pull, etc.)
- enforcing the [host lifecycle](./concepts/hosts.md#Lifecycle), including automatically stopping a host when all its agents are idle
- configuring/enabling/disabling [plugins](./concepts/plugins.md)

## Multi-user support

`mngr` typically runs as a single user on a host (it stores its data at `~/.mngr/` by convention, for example).

While it's possible to run as multiple users (esp locally), no data is shared between different users on the same machine.
This means that, when connecting to remote hosts, we need to be careful to expand the "~" in paths only once we know the user that we are running as.
