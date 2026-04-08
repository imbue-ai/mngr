<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr help

**Synopsis:**

```text
mngr help [<command> | <topic>]
```

Show help for a command or topic.

Show help for a mngr command or topic. Without arguments, lists all
available commands and help topics.

For commands, 'mngr help <command>' is equivalent to 'mngr <command> --help'.
Command aliases are supported (e.g., 'mngr help c' shows help for 'create').

For subcommands, specify the full command path (e.g., 'mngr help snapshot create').

Help topics provide documentation on concepts that span multiple commands,
such as agent address format.

**Usage:**

```text
mngr help [OPTIONS] [TOPIC]...
```
## Arguments

- `TOPIC`: The topic (optional)

**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Available Topics

- address (addr) - Agent address syntax for targeting agents and hosts
- agent_types - Agent Types
- agents - Agents
- api - mngr Plugin API
- common - Common Options
- environment_variables - Environment Variables
- hosts - Hosts
- idle_detection - Idle Detection
- multi_target - Commands that target from multiple hosts/agents
- permissions - Permissions
- plugins - Plugins
- provider_backends - Provider Backends
- providers - Provider Instances
- provisioning - Provisioning
- resource_cleanup - Resource Cleanup
- snapshot - Snapshots

## Examples

**Show help for the create command**

```bash
$ mngr help create
```

**Show help using a command alias**

```bash
$ mngr help c
```

**Show help for a subcommand**

```bash
$ mngr help snapshot create
```

**Show the address format topic**

```bash
$ mngr help address
```

**List all commands and topics**

```bash
$ mngr help
```
