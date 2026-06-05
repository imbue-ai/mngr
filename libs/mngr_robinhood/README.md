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

## Running the live SDK tests

This project contains an opt-in, live integration suite (`imbue/mngr_robinhood/test_sdk_*.py`)
that verifies the documented `query()` and `ClaudeSDKClient` interfaces of the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python.md) end-to-end against the
real API. The tests are clean-room: they import only documented public names from
`claude_agent_sdk` and assert the documented behavior.

These tests make real, paid API calls, so they are **excluded from every CI run** (via the
`sdk_live` marker, which is filtered out in `offload-modal.toml`) and only run when explicitly
opted in. Each test runs the agent in an isolated temp directory with `setting_sources=[]` so it
does not pick up this repo's `CLAUDE.md` / hooks / git state.

To run them, export a first-party `ANTHROPIC_API_KEY` and set `RUN_SDK_LIVE_TESTS=1`:

```bash
set -a; source .env; set +a   # exports ANTHROPIC_API_KEY from a local .env
just test-sdk-live
```

If `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are not both set, the suite is skipped.
