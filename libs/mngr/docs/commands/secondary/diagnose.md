<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr diagnose

**Synopsis:**

```text
mngr diagnose [DESCRIPTION] [--context-file PATH] [--clone-dir PATH] [--type TYPE]
```

Launch an agent to diagnose a bug and prepare a GitHub issue.

Launch a diagnostic agent that investigates a bug in the mngr codebase.

The agent works in a worktree of a local clone of the mngr repository
(cloned to --clone-dir, default /tmp/mngr-diagnose). It analyzes the
error, finds the root cause, and prepares a GitHub issue for user review.

Provide a description as a positional argument, a --context-file written
by the error handler, or both. If neither is provided, the agent will
ask the user for details interactively.

**Usage:**

```text
mngr diagnose [OPTIONS] [DESCRIPTION]
```
## Arguments

- `DESCRIPTION`: The description (optional)

**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--clone-dir` | path | Clone location [default: /tmp/mngr-diagnose] | None |
| `--context-file` | path | JSON file with error context (written by error handler) | None |
| `--type` | text | Agent type [default: from config] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr create](../primary/create.md) - Create an agent (full option set)

## Examples

**Diagnose a described problem**

```bash
$ mngr diagnose "create fails with spaces in path"
```

**Diagnose from error context**

```bash
$ mngr diagnose --context-file /tmp/mngr-diagnose-context-abc123.json
```

**Both description and context**

```bash
$ mngr diagnose "spaces bug" --context-file /tmp/ctx.json
```
