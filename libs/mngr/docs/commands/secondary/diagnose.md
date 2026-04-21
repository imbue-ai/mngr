<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr diagnose

**Synopsis:**

```text
mngr diagnose [--description TEXT] [--context-file PATH] [--clone-dir PATH] [CREATE_OPTIONS...]
```

Launch an agent to diagnose a bug and prepare a GitHub issue.

Launch a diagnostic agent that investigates a bug in the mngr codebase.

The agent works in a worktree of a local clone of the mngr repository
(cloned to --clone-dir, default /tmp/mngr-diagnose). It analyzes the
error, finds the root cause, and prepares a GitHub issue for user review.

Provide a description via --description, a --context-file written by the
error handler, or both. If neither is provided, the agent will ask the
user for details interactively.

Any options not recognized by diagnose are forwarded to `mngr create`, so
you can use any create option (e.g. --provider, --type, --idle-timeout).
The following flags are reserved by diagnose and cannot be passed through:
--from, --source, --transfer, --branch, --message, --message-file,
--edit-message.

**Usage:**

```text
mngr diagnose [OPTIONS] [CREATE_ARGS]...
```
## Arguments

- `CREATE_ARGS`: Additional arguments passed through

**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--description` | text | Free-text description of the problem. | None |
| `--clone-dir` | path | Clone location [default: /tmp/mngr-diagnose] | None |
| `--context-file` | path | JSON file with error context (written by error handler) | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr create](../primary/create.md) - Create an agent (full option set)

## Examples

**Diagnose a described problem**

```bash
$ mngr diagnose --description "create fails with spaces in path"
```

**Diagnose from error context**

```bash
$ mngr diagnose --context-file /tmp/mngr-diagnose-context-abc123.json
```

**Diagnose on a different provider**

```bash
$ mngr diagnose --description "modal-only bug" --provider modal
```

**Diagnose with a specific agent type**

```bash
$ mngr diagnose --description "error" --type opencode
```
