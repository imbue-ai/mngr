# Using Modal

This guide walks through running coding agents on [Modal](https://modal.com) from start to finish. It assumes you are already comfortable creating and managing local agents with mngr.

For the full list of Modal build arguments and features, see the [Modal provider reference](../core_plugins/providers/modal.md).

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

If your project has a Modal template defined in `.mngr/settings.toml`, you can use it instead of passing `--provider modal` and other flags manually:

```bash
mngr create my-agent -t modal
```

Templates bundle provider settings, build arguments, environment setup, and more into a reusable preset. See [Create Templates](../customization.md#create-templates) for how to define your own.

### Timeouts

Modal sandboxes have a default timeout of 15 minutes, after which they are terminated. For longer tasks, increase it:

```bash
mngr create my-agent --provider modal -b timeout=3600
```

The maximum is 24 hours.

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

Remote agents work in an isolated sandbox. When the agent makes changes (edits files, creates commits), those changes exist only on the remote machine. You need to transfer them back.

There are two approaches: **let the agent push via git**, or **use `mngr pull`**.

### Option A: Give the agent git credentials

If the agent has GitHub credentials, it can `git push` directly. Set up credentials by passing `GH_TOKEN` as an environment variable, or by configuring an `extra_window` in your template that runs `gh auth setup-git`.

Example template in `.mngr/settings.toml`:

```toml
[create_templates.modal]
provider = "modal"
extra_window = ["github_setup='gh auth setup-git'"]
```

Or pass the token at create time:

```bash
mngr create my-agent --provider modal --pass-env GH_TOKEN
```

Once the agent can push, your normal git workflow applies: the agent pushes to a branch, you pull it locally or open a PR.

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
