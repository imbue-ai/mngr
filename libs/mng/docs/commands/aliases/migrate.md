<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng migrate

**Synopsis:**

```text
mng migrate <SOURCE_AGENT> [<AGENT_NAME>] [create-options...]
```

Move an agent to a different host by cloning and destroying the original [experimental].

This is equivalent to running `mng clone <source>` followed by
`mng destroy --force <source>`. The first argument is the source agent to
migrate. An optional second positional argument sets the new agent's name.
All remaining arguments are passed through to the create command.

The source agent is always force-destroyed after a successful clone. If the
clone step fails, the source agent is left untouched. If the destroy step
fails after a successful clone, the error is reported and the user can
manually clean up.


## See Also

- [mng clone](./clone.md) - Clone an agent (without destroying the original)
- [mng create](../primary/create.md) - Create an agent (full option set)
- [mng destroy](../primary/destroy.md) - Destroy an agent


## Examples

**Migrate an agent to a Docker container**

```bash
$ mng migrate my-agent --provider docker
```

**Migrate with a new name**

```bash
$ mng migrate my-agent new-agent --provider modal
```

**Migrate and pass args to the agent**

```bash
$ mng migrate my-agent -- --model opus
```
