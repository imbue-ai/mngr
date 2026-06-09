<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr robinhood

**Synopsis:**

```text
mngr robinhood [CLAUDE_FLAGS...] [PROMPT]
```

Drop-in `claude -p` replacement backed by `mngr create` / `message` / `transcript`.

Run a single `claude -p`-style invocation by spawning a fresh, ephemeral
`mngr` claude agent in the current directory. The agent receives the prompt,
runs to end-of-turn, the response is collected from the agent's transcript,
and the agent is destroyed.

Almost every flag accepted by the regular `claude` CLI is forwarded verbatim
to the spawned agent. The `-p`/`--print` flag is implied (always on); the
`--input-format`, `--output-format`, `--replay-user-messages`,
`--include-partial-messages`, and `--stream-plain-text` flags are consumed by
the wrapper to shape stdin/stdout.

Streaming (approximate, reverse-mapped from the agent's tmux pane):
- `--include-partial-messages` (with `--output-format stream-json`) emits
  claude-native `text_delta` partial events as the response is produced.
- `--stream-plain-text` (with the default text output) streams the response
  text to stdout incrementally.
Both default the agent to sonnet (a user-passed `--model` still wins).

The following flags are explicitly NOT supported in v1 and will cause the
command to exit with code 2:

- --fallback-model
- --max-budget-usd
- --no-session-persistence
- --include-hook-events
- -c / --continue
- -r / --resume
- --session-id

Exit codes:
  0 - Successful turn (agent reached WAITING with a reply)
  1 - The spawned claude agent exited before completing the turn
  2 - mngr-side failure (bad flags, missing prompt, agent failed to start, etc.)

**Usage:**

```text
mngr robinhood [OPTIONS] [ARGV]...
```
## Arguments

- `ARGV`: The argv (optional)

**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## See Also

- [mngr create](../primary/create.md) - Create a long-lived mngr agent (this command's underlying primitive)
- [mngr message](./message.md) - Send a follow-up message to a running agent
- [mngr transcript](./transcript.md) - Read the message transcript for an agent

## Examples

**Single prompt**

```bash
$ mngr robinhood "summarize this repo"
```

**JSON output**

```bash
$ mngr robinhood "summarize" --output-format json
```

**Stream output**

```bash
$ mngr robinhood "explain recursion" --output-format stream-json --verbose
```

**Pipe stdin**

```bash
$ cat error.log | mngr robinhood "explain this"
```

**Multi-turn via stream-json**

```bash
$ printf '%s\n' '{"type":"user","message":{"role":"user","content":"hi"}}' | mngr robinhood --input-format stream-json --output-format stream-json
```
