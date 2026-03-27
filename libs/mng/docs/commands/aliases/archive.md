<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng archive

**Synopsis:**

```text
mng archive [AGENTS...] [--agent <AGENT>] [--all] [--dry-run] [stop-options...]
```

Stop and archive agents.

Shorthand for 'mng stop --archive'. Stops the specified agents and sets
an 'archived_at' label with the current UTC timestamp on each one.

Archived agents remain in 'mng list' output but can be filtered out
using label-based filtering. Their state is preserved (not destroyed),
so they can be restarted later if needed.

All options from the stop command are supported.


## See Also

- [mng stop](../primary/stop.md) - Stop agents without archiving
- [mng label](../secondary/label.md) - Set arbitrary labels on agents
- [mng list](../primary/list.md) - List agents (use labels to filter archived agents)
- [mng start](../primary/start.md) - Restart archived agents


## Examples

**Archive a single agent**

```bash
$ mng archive my-agent
```

**Archive multiple agents**

```bash
$ mng archive agent1 agent2
```

**Archive all running agents**

```bash
$ mng archive --all
```

**Preview what would be archived**

```bash
$ mng archive --all --dry-run
```
