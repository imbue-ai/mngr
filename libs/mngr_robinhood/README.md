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

## mngr-backed Agent SDK (`imbue.mngr_robinhood.agent_sdk`)

This package also exposes a drop-in re-implementation of the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python.md) Python surface that is
backed by mngr instead of a directly-spawned `claude` subprocess. Import `query` /
`ClaudeAgentOptions` / `ClaudeSDKClient` (and the session functions) from
`imbue.mngr_robinhood.agent_sdk` instead of from `claude_agent_sdk`; every *type* is re-exported
verbatim, so `isinstance` checks and field shapes are identical. Each session is a
`robinhood-`prefixed mngr claude agent, driven through the in-process mngr API and read back from
its native transcript.

Supported control surfaces and how they map onto mngr:

- `can_use_tool` + `hooks` — served by a local HTTP bridge: the agent is launched with a
  `--settings` file whose hook commands POST each event to the bridge, which runs the in-process
  Python callback and returns claude's hook JSON (allow / deny / `updated_input`); denials surface
  in `ResultMessage.permission_denials`.
- `interrupt()` — stops the agent mid-turn; the response stream ends at a `ResultMessage` and the
  next `query()` restarts-with-resume.
- `set_model` / `set_permission_mode` — rewrite the agent's stored launch command with the new
  configuration and restart it on the resumed session.
- `get_server_info()` — runs a one-shot `claude` stream-json probe for the real commands / output
  style, cached per session.
- `total_cost_usd` — computed from per-turn token usage times a per-model price table (approximate).

Documented limitations (real-SDK-only):

- `fork_session` raises `AgentSdkNotImplementedError`. claude's `--fork-session` does not assign a
  new session id when driven interactively over an adopted, resumed session (the forked turn is
  written under the source id), so a faithful fork cannot be produced on this transport.
- Partial-message streaming (`include_partial_messages` -> `StreamEvent`) is not available (mngr
  mirrors message-level session JSONL, not claude's token-level stream).
- The agent is not hermetic from the host's claude config -- it must load real settings to
  authenticate -- whereas the real SDK with `setting_sources=[]` is hermetic.

## Running the live SDK tests

This project contains an opt-in, live integration suite (`imbue/mngr_robinhood/test_sdk_*.py`)
that verifies the documented `query()` and `ClaudeSDKClient` interfaces of the Claude Agent SDK
end-to-end against the real API. The tests are clean-room: they import only documented public
names from `claude_agent_sdk` and assert the documented behavior.

The suite is parametrized by the `sdk` fixture over **two targets**: `real_sdk` (the real
`claude_agent_sdk` package, run via `claude --print`) and `mngr_sdk` (this module, which drives an
interactive `claude` agent in tmux). Every test runs against both unless it exercises a surface the
mngr transport cannot provide (currently only partial-message `StreamEvent` streaming), which is
gated to `real_sdk` via the `requires_native_sdk` fixture. The mngr target therefore needs a local
`claude` CLI and `tmux` on PATH. Use `-k mngr_sdk` / `-k real_sdk` to run a single target.

These tests make real, paid API calls, so they are **excluded from every CI run** (via the
`sdk_live` marker, which is filtered out in `offload-modal.toml`) and only run when explicitly
opted in. Each test runs the agent in an isolated temp directory.

To run them, export a first-party `ANTHROPIC_API_KEY` and set `RUN_SDK_LIVE_TESTS=1`:

```bash
set -a; source .env; set +a   # exports ANTHROPIC_API_KEY from a local .env
just test-sdk-live
```

If `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are not both set, the suite is skipped.
