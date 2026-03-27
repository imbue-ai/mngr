<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng clone

**Synopsis:**

```text
mng clone <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]
```

Create a new agent by cloning an existing one [experimental].

This is a convenience wrapper around `mng create --from-agent <source>`.
The first argument is the source agent to clone from. An optional second
positional argument sets the new agent's name. All remaining arguments are
passed through to the create command.


## See Also

- [mng create](../primary/create.md) - Create an agent (full option set)
- [mng list](../primary/list.md) - List existing agents


## Examples

**Clone an agent with auto-generated name**

```bash
$ mng clone my-agent
```

**Clone with a specific name**

```bash
$ mng clone my-agent new-agent
```

**Clone into a Docker container**

```bash
$ mng clone my-agent --provider docker
```

**Clone and pass args to the agent**

```bash
$ mng clone my-agent -- --model opus
```
