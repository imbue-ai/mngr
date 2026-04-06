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

| Topic | Aliases | Description |
| ----- | ------- | ----------- |
| `address` | `addr` | Agent address syntax for targeting agents and hosts |

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
