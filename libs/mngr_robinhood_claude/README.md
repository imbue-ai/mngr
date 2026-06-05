# imbue-mngr-robinhood-claude

Drop-in replacement for `claude -p` that's implemented on top of `mngr`.

The `mngr robinhood-claude` command takes the same arguments as the regular
`claude` CLI, always behaves as if `-p`/`--print` was passed, and routes
the prompt through a fresh, ephemeral `mngr` claude agent. The agent runs
in-place in the current directory, processes the prompt (or stream of
prompts), and is destroyed when the command exits.

## Install

```bash
uv tool install imbue-mngr-robinhood-claude
```

## Usage

```bash
# Single prompt, text output
mngr robinhood-claude "summarize this repo"

# Pipe stdin in
cat error.log | mngr robinhood-claude "explain this"

# Structured JSON output (claude-native shape; cost/usage fields zeroed)
mngr robinhood-claude "summarize this repo" --output-format json

# Live event stream
mngr robinhood-claude "explain recursion" --output-format stream-json --verbose

# Multi-turn via stream-json input
printf '%s\n%s\n' \
  '{"type":"user","message":{"role":"user","content":"hi"}}' \
  '{"type":"user","message":{"role":"user","content":"and again"}}' \
  | mngr robinhood-claude --input-format stream-json --output-format stream-json
```

## Flags not supported in v1

The following `claude` flags are explicitly rejected (exit code 2):

- `--fallback-model`
- `--max-budget-usd`
- `--no-session-persistence`
- `--include-hook-events`
- `--include-partial-messages`
- `-c` / `--continue`
- `-r` / `--resume`
- `--session-id`

Every other `claude` flag is forwarded verbatim to the spawned agent.
