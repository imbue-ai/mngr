# imbue-mngr-robinhood

Drop-in replacement for `claude -p` that's implemented on top of `mngr`.

The `mngr robinhood` command takes the same arguments as the regular
`claude` CLI, always behaves as if `-p`/`--print` was passed, and routes
the prompt through a fresh, ephemeral `mngr` claude agent. The agent runs
in-place in the current directory, processes the prompt (or stream of
prompts), and is destroyed when the command exits.

## Install

```bash
uv tool install imbue-mngr-robinhood
```

## Usage

```bash
# Single prompt, text output
mngr robinhood "summarize this repo"

# Pipe stdin in
cat error.log | mngr robinhood "explain this"

# Structured JSON output (claude-native shape; cost/usage fields zeroed)
mngr robinhood "summarize this repo" --output-format json

# Live event stream
mngr robinhood "explain recursion" --output-format stream-json --verbose

# Multi-turn via stream-json input
printf '%s\n%s\n' \
  '{"type":"user","message":{"role":"user","content":"hi"}}' \
  '{"type":"user","message":{"role":"user","content":"and again"}}' \
  | mngr robinhood --input-format stream-json --output-format stream-json

# Live (approximate) streaming of the response as it is produced
mngr robinhood --output-format stream-json --include-partial-messages "tell me a long story"
mngr robinhood --stream-plain-text "tell me a long story"
```

## Streaming the response

`mngr robinhood` can surface an *approximate* live view of the response, sourced
from the agent's `stream_buffer` (the tmux-based response stream; see the
`imbue-mngr-claude` README). Two opt-in flags enable it:

- `--include-partial-messages` (requires `--output-format stream-json`): emits
  claude-native `stream_event` / `text_delta` events as the response is produced,
  followed by the authoritative `assistant` message from the transcript.
- `--stream-plain-text` (text output, the default): streams the response text to
  stdout incrementally and suppresses the trailing full-text dump to avoid
  duplication.

When either flag is set, robinhood enables the streaming watcher on the spawned
agent and defaults the model to sonnet (so fast mode is off and streaming is
observable); a user-passed `--model` still takes precedence. The streamed text is
best-effort: the `result` envelope (and, in stream-json, the final `assistant`
message) remain the source of truth.

## Flags not supported in v1

The following `claude` flags are explicitly rejected (exit code 2):

- `--fallback-model`
- `--max-budget-usd`
- `--no-session-persistence`
- `--include-hook-events`
- `-c` / `--continue`
- `-r` / `--resume`
- `--session-id`

Every other `claude` flag is forwarded verbatim to the spawned agent.
