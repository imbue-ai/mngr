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
[create_templates.my-modal]
provider = "modal"
agent_args = ["--dangerously-skip-permissions"]
pass_env = ["GH_TOKEN"]
extra_window = ["github_setup='ssh-keyscan github.com >> ~/.ssh/known_hosts && git remote set-url origin https://github.com/<org>/<repo>.git && gh auth setup-git'"]
```

The `extra_window` creates a tmux window that trusts GitHub's host key, switches the remote to HTTPS (since the sandbox won't have your SSH keys), and configures git to authenticate via `gh` (which uses `GH_TOKEN`).

See [Create Templates](../customization.md#create-templates) for the full set of template options.

### Timeouts

Modal sandboxes have a default timeout of 15 minutes (900 seconds), after which they are terminated. For longer tasks, increase the timeout in seconds:

```bash
mngr create my-agent --provider modal -b timeout=3600
```

The maximum is 86400 (24 hours).

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

## Lifecycle and snapshots

`mngr connect`, `mngr message`, `mngr stop`, `mngr start`, `mngr destroy`, and `mngr list` all work the same as for local agents.

The key difference: Modal sandboxes are terminated after their timeout expires or when idle detection kicks in. `mngr stop` only stops the agent's tmux session -- the sandbox keeps running until it times out or idle detection terminates it. Before terminating, idle detection automatically takes a snapshot so that `mngr start` can restore from it. You can also create named snapshots manually:

```bash
mngr snapshot create my-agent --name before-refactor
```

Snapshots are fast, incremental, and persist after the sandbox is gone. See [mngr snapshot](../commands/secondary/snapshot.md) for details.

## What else is possible

The Modal provider supports GPUs, custom Docker images, persistent volumes, network restrictions, and more. See the [Modal provider reference](../core_plugins/providers/modal.md) for the full set of build arguments.
