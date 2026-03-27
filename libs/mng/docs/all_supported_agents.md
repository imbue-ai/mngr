# Supported Agent Types

This page provides a quick reference for all built-in agent types. For detailed information about agent types and how to create custom types, see [Agent Types](./concepts/agent_types.md).

## Built-in Agent Types

Built-in plugins provide the following agent types:

| Type | Command | Description |
|------|---------|-------------|
| `claude` | `claude` | [Claude Code](https://claude.ai/claude-code) - Anthropic's agentic coding tool. Includes session resumption support. |
| `code-guardian` | `claude` | Extends `claude` with a skill that identifies code-level inconsistencies and produces a structured report. |
| `codex` | `codex` | [Codex CLI](https://github.com/openai/codex) - OpenAI's coding assistant. |
| `fixme-fairy` | `claude` | Extends `claude` with a skill that finds and fixes a random FIXME in the codebase. |

## External Plugin Agent Types

The following agent types require installing an external plugin:

| Type | Command | Description | Plugin |
|------|---------|-------------|--------|
| `opencode` | `opencode` | [OpenCode](https://github.com/sst/opencode) - An open-source AI coding assistant. | `mng-opencode` |

## Using Agent Types

Create an agent with a specific type (AGENT_TYPE is the second positional argument):

```bash
mng create my-agent claude     # named "my-agent", type "claude"
mng create my-agent codex      # named "my-agent", type "codex"
mng create my-agent opencode   # named "my-agent", type "opencode"
```

Or use the `--type` option:

```bash
mng create my-agent --type claude
```

Any command in your PATH can also be used as an agent type:

```bash
mng create my-agent ./my-script
mng create my-agent python my_agent.py
```

## Custom Agent Types

You can define custom agent types in your config to bundle commonly-used flags or share configuration:

```bash
mng config edit
```

```toml
[agent_types.my_claude]
parent_type = "claude"
cli_args = "--env CLAUDE_MODEL=opus"
permissions = ["github"]  # [future] not yet enforced
```

For more details, see [Agent Types](./concepts/agent_types.md).
