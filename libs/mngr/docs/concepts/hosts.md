# Hosts

A host is an isolated sandbox where [agents](./agents.md) run. Each host is created by a [provider](./providers.md).

Multiple agents can share a single host.

Hosts must have at least one agent.

A host contains:
- **files**:
  - **host state_dir**: a directory where host-level state is stored (logs, etc.)
  - **agent work_dirs**: project files. Agents run in this directory.
  - **agent state_dirs**: per-agent state (status, logs, caches, etc.)
  - **...any other files**: created during provisioning or by agents themselves
- **agent tmux sessions** whose names begin with "mngr-" (one per agent, for detaching/reattaching; prefix can be changed via `MNGR_PREFIX`)
- **processes**:
  - **agent processes**: the main programs that count as agents (Claude, Codex, etc.) — one per agent, each in its own tmux session
  - **sshd**: (remote only) the SSH server (one per host)
  - **...any other services** started by plugins during provisioning (or later by agents themselves). Ex: nginx [future], frpc [future], ttyd [future], etc.

Host-level processes (nginx, frpc, sshd, ttyd) are started when the host starts.
Agent processes and their tmux sessions are created when an agent is created or started.
Agent processes also get restarted when remote hosts restart if the "start on boot" flag is set (if you want the same behavior for your local host, simply call `mngr start --include host=local` after rebooting manually or add your own boot script).

## Local vs. Remote Hosts

The "local" host is special in that it is always available for running agents via `mngr`.
Local agents are fast and easy to set up, but `mngr` cannot snapshot local hosts like desktops and laptops (only cloud hosts).

Remote hosts may be run in the cloud (Modal), a Docker container (which can be local or remote), or somewhere else. Remote hosts are *always* accessed via SSH.

## Lifecycle

The rough state diagram looks like this:

```
building  →  starting      →       running     →    stopping    →    stopped/paused/crashed
    ↓            ↓                    ↓                                   ↓
  failed   failed/stopped/crashed  stopped/paused/crashed/destroyed     destroyed
```

| State         | Description                                                                          |
|---------------|--------------------------------------------------------------------------------------|
| **building**  | Building the image, etc.                                                             |
| **starting**  | Creating and provisioning the host, starting the agent, etc.                         |
| **running**   | While any agent is running and considered active                                     |
| **stopping**  | When all agents become idle, the host is being stopped (snapshotted, host shut down) |
| **paused**    | Host became idle and was snapshotted/shut down (can be restarted)                    |
| **stopped**   | All agents exited or user explicitly stopped the host (can be restarted)             |
| **crashed**   | Host shut down unexpectedly without a controlled shutdown                            |
| **failed**    | Something went wrong before the host could be created                                |
| **destroyed** | Host gone, resources freed                                                           |

Transitional states have configurable timeouts. If exceeded, hosts auto-transition to `failed`, `stopped`, or `destroyed` (as appropriate).

Each state is covered in more detail below

### Building

A host is considered "building" while the provider is creating the host (e.g. building a Docker or Modal image)

At this point, no host exists yet. Failures here will move to the `failed` state.

### Starting

A host is "starting" while it and the agent(s) are being created and provisioned.

The high-level steps when creating a new host are:

1. **Create the host** via the provider (e.g. start a Docker container, create a Modal sandbox, etc.)
2. **Copy the files** (the work_dir, the .env files, secrets, and any other necessary files).
3. **Create the agent(s)** by making the minimal required directories and files.
4. **Provision the agent(s)** by installing packages, creating config files, etc.  See [provisioning](./provisioning.md) for more details.
5. **Start the host-level services** (nginx, frpc, sshd, ttyd).
6. **Start the agent(s)** by creating the tmux session(s) and their agent commands.

When starting a stopped host, steps 1-4 are skipped (the host already exists and has the files it needs).

`mngr` allows specifying hosts via the [devcontainer](https://containers.dev/implementors/spec/) format. This allows you to define lifecycle hooks [future] that run during startup:

1. `initializeCommand`: runs before step 1 above. Runs on the local machine where `mngr` is running.
2. `onCreateCommand`: runs after step 1 above. Runs inside the host.
3. `updateContentsCommand`: runs after 3. Runs inside the host.
4. `postCreateCommand`: runs after step 6. Only runs the first time the host is created. Runs inside the host.
5. `postStartCommand`: runs after step 6. Runs every time the host starts. Runs inside the host.

The "post" commands do *not* block--they run in the background after the agent has started.

See [the devcontainer spec](https://containers.dev/implementors/spec/) for more details on how to define these commands in your `devcontainer.json`.

### Running

A host is "running" as long as any agent is "active"

What counts as "active" depends on the "idle_mode" setting for each agent (see [idle detection](./idle_detection.md) for more details):

Once a host becomes idle, it transitions to the `stopping` state.

### Stopping

Stopping is triggered by any of the following:

1. A script that is injected and started during host startup (which periodically checks for idleness, and stops the host when idle)
2. Provider-level limits (e.g., Modal's sandbox timeout) as a backup in case the inner script fails
3. The user manually stopping the host via `mngr stop`

When a host is "stopping", it performs these steps:

1. **Stop** the agent process(es), tmux session(s), and any host-wide services (in that order, e.g., opposite of startup)
2. **Snapshot** the host (if required by the provider or enabled by the user)
3. **Shut down** the host (to free up resources)

### Stopped

While a host is "stopped", it is completely shut down and only consuming storage (for snapshots, etc.)

A host enters the stopped state when:
- The user explicitly stops the host via `mngr stop`
- All agent tmux sessions have exited (detected by the activity watcher)

You can "start" agents on a stopped host, which causes the host to transition to the `starting` state.

### Paused

While a host is "paused", it is completely shut down and only consuming storage (for snapshots, etc.)

You can "start" agents on a paused host, which causes the host to transition to the `starting` state.

### Crashed

A host is considered "crashed" if, according to the provider-specific stored state, the host *should* be running, but it is not.

Hosts that are "crashed" can be started by starting any of their agent(s)

### Failed

A host enters the "failed" state if something goes wrong during the `building` or `starting` phase *before* the host is created.

### Destroyed

A host is considered "destroyed" when either of these is true:

1. a host no longer exists according to the provider.
2. a host no longer has any agents (e.g., no agent state directories).
3. a host has a "destroy" file in its state_dir.

`mngr` does a little bit of caching to remember hosts that have been destroyed recently (so that it is easier to tell that they were destroyed), but generally once a host is destroyed, it is gone for good.

## Properties

See [host spec](../../future_specs/host.md) for the properties of hosts and their storage locations.

## Interface

See [`imbue/mngr/interfaces/host.py`](../../imbue/mngr/interfaces/host.py) for the host data structures.
