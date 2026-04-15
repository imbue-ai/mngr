# Using Modal

Run coding agents on [Modal](https://modal.com) sandboxes. For general agent management, see [mngr create](../commands/primary/create.md). For the full list of Modal build arguments, see the [Modal provider reference](../core_plugins/providers/modal.md).

## Prerequisites

- A Modal account and the `modal` CLI set up (`modal token set`)
- mngr installed and working locally

## Creating a remote agent

From any git repo:

```bash
mngr create my-agent --provider modal
```

This builds a remote sandbox on Modal and drops you into a tmux session, just like a local agent.

### Using a template

If your project has a Modal template defined in `.mngr/settings.toml`, you can use `-t modal` instead of passing flags manually:

```bash
mngr create my-agent -t modal
```

A typical Modal template includes `--dangerously-skip-permissions` since Modal sandboxes are disposable environments. This is safe for the sandbox itself, but be aware that any credentials you provide (e.g. `GH_TOKEN`) can be used by the agent without confirmation prompts.

Example template:

```toml
[create_templates.modal]
provider = "modal"
agent_args = ["--dangerously-skip-permissions"]
pass_env = ["GH_TOKEN"]
```

See [Create Templates](../customization.md#create-templates) for the full set of template options.

### Timeouts

Modal sandboxes have a default timeout of 15 minutes (900 seconds), after which they are terminated. For longer tasks, increase the timeout in seconds:

```bash
mngr create my-agent --provider modal -b timeout=3600
```

The maximum is 86400 (24 hours).

## Working with the agent

Once created, working with a remote agent is the same as a local one:

```bash
# Connect to the agent's tmux session
mngr connect my-agent

# Send a message without connecting
mngr message my-agent 'Run the test suite'

# Check status
mngr list
```

## Getting changes back

To retrieve changes from the remote sandbox, either **let the agent push via git** or **use `mngr pull`**.

### Option A: Give the agent git credentials

If the agent has `GH_TOKEN` (via `pass_env` in a template or `--pass-env` on the CLI), it can `git push` directly.

### Option B: Use `mngr pull`

`mngr pull` transfers changes from the agent to your local machine without needing git credentials on the agent. It supports two sync modes:

**Pull git commits** (when the agent has committed its work):

```bash
mngr pull my-agent --sync-mode=git
```

This merges the agent's branch into your current local branch.

**Pull files** (default -- works for uncommitted changes and non-git-tracked files):

```bash
mngr pull my-agent
```

This uses rsync to sync the agent's working directory to your current directory. To preview what would be transferred first:

```bash
mngr pull my-agent --dry-run
```

You can also pull a specific subdirectory:

```bash
mngr pull my-agent:src ./local-src
```

To push local changes to the agent (e.g. a config file you edited locally):

```bash
mngr push my-agent:config ./config
```

See [mngr pull](../commands/primary/pull.md) and [mngr push](../commands/primary/push.md) for all options.

## Stopping and restarting

`mngr stop` stops the agent's tmux session, but the Modal sandbox itself keeps running (and continues consuming resources) until it is terminated by timeout, idle detection, or `mngr destroy`.

To preserve state across sandbox termination, take a snapshot first (see [Snapshots](#snapshots)). `mngr start` restores from the most recent snapshot — without one, state is not preserved if the sandbox is terminated.

```bash
# Take a snapshot to preserve state, then stop the agent session
mngr snapshot create my-agent
mngr stop my-agent

# Later: restore from snapshot and reconnect
mngr start my-agent
mngr connect my-agent
```

## Snapshots

Modal supports native filesystem snapshots, which are fast and incremental:

```bash
mngr snapshot create my-agent
mngr snapshot create my-agent --name before-refactor
mngr snapshot list my-agent
```

Snapshots persist even after the sandbox is terminated. See [mngr snapshot](../commands/secondary/snapshot.md) for details.

## Cleanup

```bash
mngr destroy my-agent
```

## What else is possible

The Modal provider supports GPUs, custom Docker images, persistent volumes, network restrictions, and more. See the [Modal provider reference](../core_plugins/providers/modal.md) for the full set of build arguments.
