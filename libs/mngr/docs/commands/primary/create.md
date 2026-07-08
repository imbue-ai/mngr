<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr create

**Synopsis:**

```text
mngr [create|c] [<ADDRESS>] [<AGENT_TYPE>] [-t <TEMPLATE>] [--new-host] [-w WINDOW_NAME=COMMAND]
    [--label KEY=VALUE] [--host-label KEY=VALUE] [--project <PROJECT>] [--from <SOURCE>] [--adopt <SESSION>] [--transfer <MODE>]
    [--[no-]rsync] [--rsync-args <ARGS>] [--branch [BASE][:NEW]] [--[no-]ensure-clean]
    [--snapshot <ID>] [-b <BUILD_ARG>] [-s <START_ARG>] [--post-host-create-command <COMMAND>] [--post-host-create-outer-command <COMMAND>]
    [--env <KEY=VALUE>] [--env-file <FILE>] [--pass-env <KEY>] [--extra-provision-command <COMMAND>] [--upload-file <LOCAL:REMOTE>]
    [--idle-timeout <SECONDS>] [--idle-mode <MODE>] [--start-on-boot|--no-start-on-boot] [--reuse|--no-reuse]
    [--message <TEXT>] [--message-file <FILE>] [--edit-message]
    [--[no-]connect] [--[no-]auto-start] [-y|--yes] [--] [<AGENT_ARGS>...]
```

Create and run an agent.

This command sets up an agent's working directory, optionally provisions a
new host (or uses an existing one), runs the specified agent process, and
connects to it by default.

By default, agents run locally in a new git worktree (for git repositories)
or an rsync copy (for non-git projects). Specify a host in the agent address
(e.g. NAME@HOST.PROVIDER) to target a remote host, or use NAME@.PROVIDER
to create a new one.

Arguments after -- are passed directly to the agent command. To run an
arbitrary shell command, use the built-in 'command' agent type:
`mngr create my-task --type command -- sleep 3600`.

Headless agent types (those implementing StreamingHeadlessAgentMixin,
like headless_command and headless_claude) require the --foreground flag.
The agent streams its output to stdout and is destroyed when done instead
of being connected to.

When the source and the agent are on the same host (local or a single remote
provider host), mngr creates a git worktree that shares objects with the source
repository. When they are on different hosts, the repo is transferred by
pushing all local branches and tags via git. Use --transfer to override the default.

Alias: c

**Usage:**

```text
mngr create [OPTIONS] [POSITIONAL_NAME] [POSITIONAL_AGENT_TYPE] [AGENT_ARGS]...
```
## Arguments

- `ADDRESS`: Agent address in `[NAME][@[HOST][.PROVIDER]][:PATH]` format (all parts optional):
  - `NAME` -- agent name only, creates on local host (default)
  - `NAME@HOST` -- agent on existing host
  - `NAME@HOST.PROVIDER` -- agent on existing host (with provider for disambiguation)
  - `NAME@.PROVIDER` -- agent on a new host (auto-generated host name); implies `--new-host`
  - `NAME@HOST.PROVIDER --new-host` -- agent on a new host with the given name
  - `NAME:PATH` -- agent with a target path for the working directory
  - `:PATH` -- auto-named agent with a target path (equivalent to omitting the name)
- `AGENT_TYPE`: Which type of agent to run. Can also be specified via `--type`.
- `AGENT_ARGS`: Additional arguments passed to the agent

**Options:**

## Agent Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-t`, `--template` | text | Use a named template from create_templates config [repeatable, stacks in order] | None |
| `-n`, `--name` | new_agent_location | Agent address (alternative to positional argument, mutually exclusive) [default: auto-generated] | None |
| `--id` | text | Explicit agent ID [default: auto-generated] | None |
| `--name-style` | choice (`coolname` &#x7C; `english` &#x7C; `fantasy` &#x7C; `scifi` &#x7C; `painters` &#x7C; `authors` &#x7C; `artists` &#x7C; `musicians` &#x7C; `animals` &#x7C; `scientists` &#x7C; `demons`) | Auto-generated name style | `coolname` |
| `--type` | text | Which type of agent to run | None |
| `-w`, `--extra-window` | text | Run extra command in additional window. Use name="command" to set window name. Note: ALL_UPPERCASE names (e.g., FOO="bar") are treated as env var assignments, not window names | None |
| `--label` | text | Agent label KEY=VALUE [repeatable] [experimental] | None |
| `--project` | text | Project name for the agent (sets the 'project' label; '.' inherits from source agent's project label when --from references an agent, else uses the source's git remote origin, else the source's folder name) [default: .] | `.` |
| `--tmux-width` | integer | Width (columns) of the agent's tmux window [default: 200] | None |
| `--tmux-height` | integer | Height (rows) of the agent's tmux window [default: 50] | None |
| `--tmux-window-size` | choice (`manual` &#x7C; `latest` &#x7C; `largest` &#x7C; `smallest`) | tmux window resize policy; 'manual' pins the window to its width/height and never resizes on attach [default: latest] | None |

## Host Options

By default, `mngr create` uses the local host. Use the agent address to specify a different host.

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Provider for the host (alternative to .PROVIDER in the address, e.g. --provider docker) | None |
| `--new-host` | boolean | Force creating a new host (requires a provider via address or --provider) | `False` |
| `--host-label` | text | Host metadata label KEY=VALUE [repeatable] | None |
| `--host-name-style` | choice (`coolname` &#x7C; `astronomy` &#x7C; `places` &#x7C; `cities` &#x7C; `fantasy` &#x7C; `scifi` &#x7C; `painters` &#x7C; `authors` &#x7C; `artists` &#x7C; `musicians` &#x7C; `scientists`) | Auto-generated host name style | `coolname` |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--reuse`, `--no-reuse` | boolean | Reuse existing agent with the same name if it exists (idempotent create) | `False` |
| `--update`, `--no-update` | boolean | When combined with --reuse, stop and fully re-create the agent (update work_dir, re-provision, restart). Requires --reuse | `False` |
| `--connect`, `--no-connect` | boolean | Connect to the agent after creation [default: connect] | `True` |
| `--foreground` | boolean | Run a headless agent in the foreground, streaming output and auto-destroying when done. Required for headless agent types | `False` |
| `--auto-start`, `--no-auto-start` | boolean | Automatically start offline hosts (source and target) before proceeding | `True` |

## Source Data (what to include in the new agent)

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--from`, `--source` | text | Source data for the agent [AGENT[@HOST[.PROVIDER]][:PATH] &#x7C; @HOST:PATH &#x7C; :PATH &#x7C; GIT_URL]. A bare name refers to an agent; use :PATH for a directory. GIT_URL (e.g. https://github.com/owner/repo or git@gitlab.com:owner/repo.git) is cloned to ~/.mngr/clones/<name>-<id>/ using local git auth. Defaults to git root if omitted | None |
| `--adopt`, `--adopt-session` | text | Adopt an existing session into this newly created agent so it resumes that conversation. Accepts a session id or a path to the session file; a session id is searched across the relevant user/config store, every live local mngr agent, and preserved sessions from destroyed agents. Repeatable: every named session is copied in, and the last is resumed on startup (unless combined with --from, in which case the source agent's session is resumed). | None |
| `--rsync`, `--no-rsync` | boolean | Use rsync for file transfer [default: yes if rsync-args are present or if git is disabled] | None |
| `--rsync-args` | text | Additional arguments to pass to rsync | None |

## Target (where to put the new agent)

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--target-path` | text | Directory to mount source inside agent host (alternative to :PATH in address). Incompatible with --transfer=none | None |
| `--transfer` | choice (`none` &#x7C; `rsync` &#x7C; `git-mirror` &#x7C; `git-worktree`) | How to transfer the project into the agent. none: run in-place (no transfer). rsync: copy via rsync (non-git projects). git-mirror: push all local branches and tags via git (git projects). git-worktree: create a git worktree (git projects; source and target must be on the same host). [default: git-worktree when source and target are on the same host (local or remote), git-mirror for cross-host git repos, rsync for non-git] | None |

## Git Configuration

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--branch` | text | Branch spec as [BASE][:NEW]. BASE defaults to current branch. NEW creates a fresh branch (* is replaced by agent name). Omit :NEW to use BASE directly without creating a branch. Empty NEW (e.g. 'main:') defaults to mngr/*. | `:mngr/*` |
| `--ensure-clean`, `--no-ensure-clean` | boolean | Abort if working tree is dirty | `True` |
| `--include-unclean`, `--exclude-unclean` | boolean | Include uncommitted files [default: include if --no-ensure-clean] | None |
| `--include-gitignored`, `--no-include-gitignored` | boolean | Include gitignored files | `False` |
| `--worktree-base-folder` | path | Base folder for git worktrees [default: <host_dir>/worktrees] | None |

## Environment Variables

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--env` | text | Set environment variable KEY=VALUE | None |
| `--env-file` | path | Load env | None |
| `--pass-env` | text | Forward variable from shell | None |

## Provisioning

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--extra-provision-command` | text | Run custom shell command during provisioning [repeatable] | None |
| `--upload-file` | text | Upload LOCAL:REMOTE file pair [repeatable] | None |

## New Host Environment Variables

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--host-env` | text | Set environment variable KEY=VALUE for host [repeatable] | None |
| `--host-env-file` | path | Load env file for host [repeatable] | None |
| `--pass-host-env` | text | Forward variable from shell for host [repeatable] | None |

## New Host Build

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--snapshot` | text | Use existing snapshot instead of building | None |
| `-b`, `--build-arg` | text | Build argument as key=value or --key=value (e.g., -b gpu=h100 -b cpu=2) [repeatable] | None |
| `-s`, `--start-arg` | text | Argument for start [repeatable] | None |
| `--post-host-create-command` | text | Shell command to run inside the new host after it is created, before any agent work_dir setup. Runs synchronously; non-zero exit aborts the create. [repeatable] | None |
| `--post-host-create-outer-command` | text | Shell command to run once on the host's outer machine (the underlying VM/daemon host) after the host is created. Runs synchronously; non-zero exit aborts the create. Skipped (with a warning) when the provider has no outer host. [repeatable] | None |

## Host Lifecycle

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--idle-timeout` | text | Shutdown after idle for specified duration (e.g., 30s, 5m, 1h, or plain seconds) [default: none] | None |
| `--idle-mode` | choice (`io` &#x7C; `user` &#x7C; `agent` &#x7C; `ssh` &#x7C; `create` &#x7C; `boot` &#x7C; `start` &#x7C; `run` &#x7C; `custom` &#x7C; `disabled`) | When to consider host idle [default: io if remote, disabled if local] | None |
| `--activity-sources` | text | Activity sources for idle detection (comma-separated) | None |
| `--start-on-boot`, `--no-start-on-boot` | boolean | Restart on host boot | `False` |

## Connection Options

See [connect options](./connect.md) for full details (only applies if `--connect` is specified).

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--reconnect`, `--no-reconnect` | boolean | Automatically reconnect if dropped | `True` |
| `--message` | text | Initial message to send after the agent starts | None |
| `--message-file` | path | File containing initial message to send | None |
| `--edit-message` | boolean | Open an editor to compose the initial message (uses $EDITOR). Editor runs in parallel with agent creation. If --message or --message-file is provided, their content is used as initial editor content. | `False` |
| `--session-command` | text | Command to run instead of attaching to main session | None |
| `--connect-command` | text | Command to run instead of the builtin connect. MNGR_AGENT_NAME and MNGR_SESSION_NAME env vars are set. | None |

## Automation

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-y`, `--yes` | boolean | Auto-approve all prompts (e.g., skill installation) without asking | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Provider Build/Start Arguments

Provider: aws
  EC2-specific args (consumed by provider, not passed to docker):
    --aws-region=REGION         Must match the provider config's default_region;
                                the client is bound to one region at construction
                                and refuses cross-region creates. To target multiple
                                regions, define one [providers.aws-<region>] block
                                per region (see mngr_aws README 'Multiple regions').
    --aws-instance-type=TYPE    EC2 instance type (default: t3.small)
    --aws-ami=AMI-ID            Override the per-host AMI for this create only
                                (default: provider config's default_ami_id, or the
                                pinned per-region default for the chosen region)
    --aws-spot                  Run on EC2 spot capacity (presence-only flag).
                                AWS may reclaim with ~2 min notice; the host is
                                terminated, not stopped, on reclaim. Opt-in only.
    --git-depth=N               Shallow-clone build context to depth N before upload

  All other build args are passed to 'docker build' on the EC2 instance.
  Example: -b --aws-instance-type=t3.medium -b --file=Dockerfile -b .
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

Provider: azure
  Azure-specific args (consumed by provider, not passed to docker):
    --azure-region=REGION       Azure region / location (default: westus)
    --azure-vm-size=SIZE        Azure VM size (default: Standard_B2s)
    --azure-spot                Run on Azure Spot capacity (presence-only flag).
                                Azure may reclaim on capacity pressure; the host is
                                deleted, not stopped, on eviction. Opt-in only.
    --git-depth=N               Shallow-clone build context to depth N before upload

  All other build args are passed to 'docker build' on the VM.
  Example: -b --azure-vm-size=Standard_D2s_v5 -b --file=Dockerfile -b .
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

Provider: docker
  Build args are passed directly to 'docker build'. Run 'docker build --help' for details.
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

Provider: gcp
  GCE-specific args (consumed by provider, not passed to docker):
    --gcp-zone=ZONE          GCE zone, e.g. us-west1-a (GCE VMs are zonal; must equal
                             the provider's configured zone; defaults to the config's
                             default_zone, the active gcloud compute/zone, or us-west1-a)
    --gcp-machine-type=TYPE  GCE machine type (default: e2-small)
    --gcp-image=IMAGE        GCE boot-disk source image for this host, overriding the
                             config's default_source_image (a full image / family URL)
    --gcp-spot               Run on GCE Spot capacity (presence-only flag; preemptible).
    --git-depth=N            Shallow-clone build context to depth N before upload

  When --gcp-image is omitted the VM image is taken from the provider config
  (default_source_image).

  All other build args are passed to 'docker build' on the GCE instance.
  Example: -b --gcp-machine-type=e2-medium -b --file=Dockerfile -b .
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

Provider: imbue_cloud
  Build args constrain which pool host the connector leases for this `mngr create`. Recognized keys (see LeaseAttributes): repo_url, repo_branch_or_tag, cpus, memory_gb, gpu_count. Unknown keys are rejected. Example: `mngr create my-agent@my-host.imbue_cloud_alice --new-host -b cpus=4 -b repo_branch_or_tag=v1.2.3`.
  Start args are not used by the imbue_cloud provider.

Provider: imbue_cloud_slice
  Slice args are passed through to the shared vps_docker bake (e.g. --file=Dockerfile, the build context).
  Start args are passed directly to 'docker run' inside the slice VM.

Provider: lima
  Supported build arguments for the lima provider:
    --file PATH           Path to a Lima YAML config file for full VM customization.
                          When not specified, a default config is generated with the
                          mngr pre-built image.
  Start args are passed directly to 'limactl start'. Common options:
    --cpus=N              Number of CPU cores (default: 4)
    --memory=N            Memory in GiB (default: 4)
    --disk=N              Disk in GiB (default: 100)
    --vm-type=TYPE        VM type: qemu or vz (default: auto-detected)
    --mount-writable      Make default mounts writable
  Run 'limactl start --help' for the full list.

Provider: local
  No build arguments are supported for the local provider.
  No start arguments are supported for the local provider.

Provider: modal
  Supported build arguments for the modal provider:
    --file PATH           Path to the Dockerfile to build the sandbox image. Default: Dockerfile in context dir
    --context-dir PATH    Build context directory for Dockerfile COPY/ADD instructions. Default: Dockerfile's directory
    --cpu COUNT           Number of CPU cores (0.25-16). Default: 1.0
    --memory GB           Memory in GB (0.5-32). Default: 1.0
    --gpu TYPE            GPU type to use (e.g., t4, a10g, a100, any). Default: no GPU
    --image NAME          Base Docker image to use. Not required if using --file. Default: debian:bookworm-slim
    --timeout SEC         Maximum sandbox lifetime in seconds. Default: 900 (15 min)
    --region NAME         Region to run the sandbox in (e.g., us-east, us-west, eu-west). Default: auto
    --secret VAR          Pass an environment variable as a secret to the image build. The value of
                          VAR is read from your current environment and made available during Dockerfile
                          RUN commands via --mount=type=secret,id=VAR. Can be specified multiple times.
    --offline             Block all outbound network access from the sandbox [experimental]. Default: off
    --cidr-allowlist CIDR Restrict network access to the specified CIDR range (e.g., 203.0.113.0/24) [experimental].
                          Can be specified multiple times.
    --volume NAME:PATH    Mount a persistent Modal Volume at PATH inside the sandbox [experimental]. NAME is the
                          volume name on Modal (created if it doesn't exist). Can be specified
                          multiple times.
    --docker-build-arg KEY=VALUE
                          Override a Dockerfile ARG default value. For example,
                          --docker-build-arg=CLAUDE_CODE_VERSION=2.1.50 sets the CLAUDE_CODE_VERSION
                          ARG during the image build. Can be specified multiple times.
  No start arguments are supported for the modal provider.

Provider: ovh
  OVH-specific args (consumed by provider, not passed to docker):
    --ovh-datacenter=DC   OVH datacenter (e.g. US-EAST-VA, US-WEST-OR)
                          (alias: --ovh-region=)
    --ovh-plan=PLAN       OVH plan code (default: vps-2025-model1 = VPS-1)
    --git-depth=N         Shallow-clone build context to depth N before upload

  All other build args are passed to 'docker build' on the VPS.
  Example: -b --ovh-plan=vps-2025-model1 -b --file=Dockerfile -b .
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

Provider: ssh
  The SSH provider does not support creating hosts dynamically.
  Hosts must be pre-configured in the mngr config file.

  Example configuration in mngr.toml:
    [providers.my-ssh-pool]
    backend = "ssh"

    [providers.my-ssh-pool.hosts.server1]
    address = "192.168.1.100"
    port = 22
    user = "root"
    key_file = "~/.ssh/id_ed25519"
  No start arguments are supported for the SSH provider.

Provider: vultr
  Vultr-specific args (consumed by provider, not passed to docker):
    --vultr-region=REGION  Vultr region (default: ewr)
    --vultr-plan=PLAN      Vultr plan (default: vc2-2c-4gb)
    --git-depth=N          Shallow-clone build context to depth N before upload

  All other build args are passed to 'docker build' on the VPS.
  Example: -b --vultr-plan=vc2-2c-4gb -b --file=Dockerfile -b .
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.


## See Also

- [mngr connect](./connect.md) - Connect to an existing agent
- [mngr list](./list.md) - List existing agents
- [mngr destroy](./destroy.md) - Destroy agents
- [mngr limit](../secondary/limit.md) - Configure agent resource limits

## Examples

**Create an agent locally in a new git worktree (default)**

```bash
$ mngr create my-agent
```

**Create an agent in a new Docker container**

```bash
$ mngr create my-agent@.docker
```

**Create an agent in a new Modal sandbox**

```bash
$ mngr create my-agent@.modal
```

**Create using a named template**

```bash
$ mngr create my-agent --template modal
```

**Stack multiple templates**

```bash
$ mngr create my-agent -t modal -t codex
```

**Create a codex agent instead of the default**

```bash
$ mngr create my-agent codex
```

**Pass arguments to the agent**

```bash
$ mngr create my-agent -- --model opus
```

**Create on an existing host**

```bash
$ mngr create my-agent@my-dev-box
```

**Create on existing host with provider**

```bash
$ mngr create my-agent@my-dev-box.modal
```

**Create a new named host**

```bash
$ mngr create my-agent@my-host.modal --new-host
```

**Clone from an existing agent**

```bash
$ mngr create new-agent --source other-agent
```

**Run directly in-place (no transfer)**

```bash
$ mngr create my-agent --transfer=none
```

**Create without connecting**

```bash
$ mngr create my-agent --no-connect
```

**Add extra tmux windows**

```bash
$ mngr create my-agent -w server="npm run dev"
```

**Reuse existing agent or create if not found**

```bash
$ mngr create my-agent --reuse
```

**Run a headless agent**

```bash
$ mngr create --type headless_command --foreground -t my-command-template
```
