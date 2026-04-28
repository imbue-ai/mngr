<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr clone

**Synopsis:**

```text
mngr clone <SOURCE> [<AGENT_NAME>] [create-options...]
```

Create a new agent by cloning an existing agent or git URL [experimental].

This is a convenience wrapper around `mngr create --from <source>`.
The first argument is the source to clone from: an existing agent, or a git
URL (https, ssh, or SCP-like form). An optional second positional argument
sets the new agent's name. All remaining arguments are passed through to the
create command.


## See Also

- [mngr create](../primary/create.md) - Create an agent (full option set)
- [mngr list](../primary/list.md) - List existing agents


## Examples

**Clone an agent with auto-generated name**

```bash
$ mngr clone my-agent
```

**Clone with a specific name**

```bash
$ mngr clone my-agent new-agent
```

**Clone into a Docker container**

```bash
$ mngr clone my-agent --provider docker
```

**Clone and pass args to the agent**

```bash
$ mngr clone my-agent -- --model opus
```

**Clone from a git URL**

```bash
$ mngr clone https://github.com/owner/repo new-agent
```
