<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng create

**Synopsis:**

```text
mng [create|c] [<AGENT_NAME>] [<AGENT_TYPE>] [-t <TEMPLATE>] [--in <PROVIDER>] [--host <HOST>] [-w WINDOW_NAME=COMMAND]
    [--label KEY=VALUE] [--host-label KEY=VALUE] [--project <PROJECT>] [--from <SOURCE>] [--in-place|--copy|--clone|--worktree]
    [--[no-]rsync] [--rsync-args <ARGS>] [--branch [BASE][:NEW]] [--[no-]ensure-clean]
    [--snapshot <ID>] [-b <BUILD_ARG>] [-s <START_ARG>]
    [--env <KEY=VALUE>] [--env-file <FILE>] [--grant <PERMISSION>] [--user-command <COMMAND>] [--upload-file <LOCAL:REMOTE>]
    [--idle-timeout <SECONDS>] [--idle-mode <MODE>] [--start-on-boot|--no-start-on-boot] [--reuse|--no-reuse]
    [--[no-]connect] [--[no-]auto-start] [--] [<AGENT_ARGS>...]
```

Create and run an agent.

This command sets up an agent's working directory, optionally provisions a
new host (or uses an existing one), runs the specified agent process, and
connects to it by default.

By default, agents run locally in a new git worktree (for git repositories)
or a copy of the current directory. Use --in to create a new remote host,
or --host to use an existing host.

The agent type defaults to 'claude' if not specified. Any command in your
PATH can also be used as an agent type. Arguments after -- are passed
directly to the agent command.

For local agents, mng creates a git worktree that shares objects with your
original repository, allowing efficient branch management. For remote agents,
the working directory is copied to the remote host.

Alias: c

**Usage:**

```text
mng create [OPTIONS] [POSITIONAL_NAME] [POSITIONAL_AGENT_TYPE] [AGENT_ARGS]...
```
## Arguments

- `NAME`: Name for the agent (auto-generated if not provided)
- `AGENT_TYPE`: Which type of agent to run (default: `claude`). Can also be specified via `--type`
- `AGENT_ARGS`: Additional arguments passed to the agent

**Options:**

## Agent Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-t`, `--template` | text | Use a named template from create_templates config [repeatable, stacks in order] | None |
| `-n`, `--name` | text | Agent name (alternative to positional argument) [default: auto-generated] | None |
| `--id` | text | Explicit agent ID [default: auto-generated] | None |
| `--name-style` | choice (`english` &#x7C; `fantasy` &#x7C; `scifi` &#x7C; `painters` &#x7C; `authors` &#x7C; `artists` &#x7C; `musicians` &#x7C; `animals` &#x7C; `scientists` &#x7C; `demons`) | Auto-generated name style | `english` |
| `--type` | text | Which type of agent to run [default: claude] | None |
| `--command` | text | Run a literal command using the generic agent type (mutually exclusive with --type) | None |
| `-w`, `--extra-window` | text | Run extra command in additional window. Use name="command" to set window name. Note: ALL_UPPERCASE names (e.g., FOO="bar") are treated as env var assignments, not window names | None |
| `--label` | text | Agent label KEY=VALUE [repeatable] [experimental] | None |

## Host Options

By default, `mng create` uses the "local" host. Use these options to change that behavior.

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--in`, `--new-host` | text | Create a new host using provider (docker, modal, ...) | None |
| `--host`, `--target-host` | text | Use an existing host (by name or ID) [default: local] | None |
| `--project` | text | Project name for the agent (sets the 'project' label) [default: derived from git remote origin or folder name] | None |
| `--host-label` | text | Host metadata label KEY=VALUE [repeatable] | None |
| `--host-name` | text | Name for the new host | None |
| `--host-name-style` | choice (`astronomy` &#x7C; `places` &#x7C; `cities` &#x7C; `fantasy` &#x7C; `scifi` &#x7C; `painters` &#x7C; `authors` &#x7C; `artists` &#x7C; `musicians` &#x7C; `scientists`) | Auto-generated host name style | `astronomy` |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--reuse`, `--no-reuse` | boolean | Reuse existing agent with the same name if it exists (idempotent create) | `False` |
| `--connect`, `--no-connect` | boolean | Connect to the agent after creation [default: connect] | `True` |
| `--auto-start`, `--no-auto-start` | boolean | Automatically start offline hosts (source and target) before proceeding | `True` |

## Agent Source Data (what to include in the new agent)

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--from`, `--source` | text | Directory to use as work_dir root [AGENT &#x7C; AGENT.HOST &#x7C; AGENT.HOST:PATH &#x7C; HOST:PATH]. Defaults to current dir if no other source args are given | None |
| `--source-agent`, `--from-agent` | text | Source agent for cloning work_dir | None |
| `--source-host` | text | Source host | None |
| `--source-path` | text | Source path | None |
| `--rsync`, `--no-rsync` | boolean | Use rsync for file transfer [default: yes if rsync-args are present or if git is disabled] | None |
| `--rsync-args` | text | Additional arguments to pass to rsync | None |
| `--include-git`, `--no-include-git` | boolean | Include .git directory | `True` |

## Agent Target (where to put the new agent)

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--target` | text | Target [HOST][:PATH]. Defaults to current dir if no other target args are given | None |
| `--target-path` | text | Directory to mount source inside agent host. Incompatible with --in-place | None |
| `--in-place` | boolean | Run directly in source directory. Incompatible with --target-path | `False` |
| `--copy` | boolean | Copy source to isolated directory before running [default for remote agents, and for local agents if not in a git repo] | `False` |
| `--clone` | boolean | Create a git clone that shares objects with original repo (only works for local agents) | `False` |
| `--worktree` | boolean | Create a git worktree that shares objects and index with original repo [default for local agents in a git repo]. Requires a new branch in --branch (which is the default) | `False` |

## Agent Git Configuration

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--branch` | text | Branch spec as [BASE][:NEW]. BASE defaults to current branch. NEW creates a fresh branch (* is replaced by agent name). Omit :NEW to use BASE directly without creating a branch. Empty NEW (e.g. 'main:') defaults to mng/*. | `:mng/*` |
| `--depth` | integer | Shallow clone depth [default: full] | None |
| `--shallow-since` | text | Shallow clone since date | None |
| `--ensure-clean`, `--no-ensure-clean` | boolean | Abort if working tree is dirty | `True` |
| `--include-unclean`, `--exclude-unclean` | boolean | Include uncommitted files [default: include if --no-ensure-clean] | None |
| `--include-gitignored`, `--no-include-gitignored` | boolean | Include gitignored files | `False` |

## Agent Environment Variables

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--env` | text | Set environment variable KEY=VALUE | None |
| `--env-file` | path | Load env | None |
| `--pass-env` | text | Forward variable from shell | None |

## Agent Provisioning

See [Provision Options](../secondary/provision.md) for full details.

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--grant` | text | Grant a permission to the agent [repeatable] | None |
| `--user-command` | text | Run custom shell command during provisioning [repeatable] | None |
| `--sudo-command` | text | Run custom shell command as root during provisioning [repeatable] | None |
| `--upload-file` | text | Upload LOCAL:REMOTE file pair [repeatable] | None |
| `--append-to-file` | text | Append REMOTE:TEXT to file [repeatable] | None |
| `--prepend-to-file` | text | Prepend REMOTE:TEXT to file [repeatable] | None |

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

## Host Lifecycle

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--idle-timeout` | text | Shutdown after idle for specified duration (e.g., 30s, 5m, 1h, or plain seconds) [default: none] | None |
| `--idle-mode` | choice (`io` &#x7C; `user` &#x7C; `agent` &#x7C; `ssh` &#x7C; `create` &#x7C; `boot` &#x7C; `start` &#x7C; `run` &#x7C; `custom` &#x7C; `disabled`) | When to consider host idle [default: io if remote, disabled if local] | None |
| `--activity-sources` | text | Activity sources for idle detection (comma-separated) | None |
| `--start-on-boot`, `--no-start-on-boot` | boolean | Restart on host boot [default: no] | None |

## Connection Options

See [connect options](./connect.md) for full details (only applies if `--connect` is specified).

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--reconnect`, `--no-reconnect` | boolean | Automatically reconnect if dropped | `True` |
| `--interactive`, `--no-interactive` | boolean | Enable interactive mode [default: yes if TTY] | None |
| `--message` | text | Initial message to send after the agent starts | None |
| `--message-file` | path | File containing initial message to send | None |
| `--edit-message` | boolean | Open an editor to compose the initial message (uses $EDITOR). Editor runs in parallel with agent creation. If --message or --message-file is provided, their content is used as initial editor content. | `False` |
| `--retry` | integer | Number of connection retries | `3` |
| `--retry-delay` | text | Delay between retries (e.g., 5s, 1m) | `5s` |
| `--attach-command` | text | Command to run instead of attaching to main session | None |
| `--connect-command` | text | Command to run instead of the builtin connect. MNG_AGENT_NAME and MNG_SESSION_NAME env vars are set. | None |

## Automation

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-y`, `--yes` | boolean | Auto-approve all prompts (e.g., skill installation) without asking | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Agent Limits

See [Limit Options](../secondary/limit.md)


## Provider Build/Start Arguments

Provider: docker
  Build args are passed directly to 'docker build'. Run 'docker build --help' for details.
  Start args are passed directly to 'docker run'. Run 'docker run --help' for details.

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

Provider: ssh
  The SSH provider does not support creating hosts dynamically.
  Hosts must be pre-configured in the mng config file.

  Example configuration in mng.toml:
    [providers.my-ssh-pool]
    backend = "ssh"

    [providers.my-ssh-pool.hosts.server1]
    address = "192.168.1.100"
    port = 22
    user = "root"
    key_file = "~/.ssh/id_ed25519"
  No start arguments are supported for the SSH provider.


## See Also

- [mng connect](./connect.md) - Connect to an existing agent
- [mng list](./list.md) - List existing agents
- [mng destroy](./destroy.md) - Destroy agents

## Examples

**Create an agent locally in a new git worktree (default)**

```bash
$ mng create my-agent
```

**Create an agent in a Docker container**

```bash
$ mng create my-agent --in docker
```

**Create an agent in a Modal sandbox**

```bash
$ mng create my-agent --in modal
```

**Create using a named template**

```bash
$ mng create my-agent --template modal
```

**Stack multiple templates**

```bash
$ mng create my-agent -t modal -t codex
```

**Create a codex agent instead of claude**

```bash
$ mng create my-agent codex
```

**Pass arguments to the agent**

```bash
$ mng create my-agent -- --model opus
```

**Create on an existing host**

```bash
$ mng create my-agent --host my-dev-box
```

**Clone from an existing agent**

```bash
$ mng create new-agent --source other-agent
```

**Run directly in-place (no worktree)**

```bash
$ mng create my-agent --in-place
```

**Create without connecting**

```bash
$ mng create my-agent --no-connect
```

**Add extra tmux windows**

```bash
$ mng create my-agent -w server="npm run dev"
```

**Reuse existing agent or create if not found**

```bash
$ mng create my-agent --reuse
```
