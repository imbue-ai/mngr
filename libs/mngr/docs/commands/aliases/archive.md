<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr archive

**Synopsis:**

```text
mngr archive [AGENTS...] [--agent <AGENT>] [--all] [-f|--force] [--dry-run]
```

Archive agents (set the 'archived_at' label).

Sets an 'archived_at' label with the current UTC timestamp on each
targeted agent. By default, only non-running agents are archived; running
agents are skipped with a warning.

Use --force to stop running agents before archiving them.

Archived agents remain in 'mngr list' output but can be filtered out
using label-based filtering. Their state is preserved (not destroyed),
so they can be restarted later if needed.


## See Also

- [mngr stop](../primary/stop.md) - Stop agents without archiving
- [mngr label](../secondary/label.md) - Set arbitrary labels on agents
- [mngr list](../primary/list.md) - List agents (use labels to filter archived agents)
- [mngr start](../primary/start.md) - Restart archived agents


## Examples

**Archive a stopped agent**

```bash
$ mngr archive my-agent
```

**Archive multiple agents**

```bash
$ mngr archive agent1 agent2
```

**Force-stop and archive a running agent**

```bash
$ mngr archive my-agent --force
```

**Archive all non-running agents**

```bash
$ mngr archive --all
```

**Force-stop and archive all agents**

```bash
$ mngr archive --all --force
```

**Preview what would be archived**

```bash
$ mngr archive --all --dry-run
```
