# imbue-mngr-robinhood

`robinhood` is a drop-in replacement for `claude -p` that's implemented on top of `mngr`, 
i.e., you can use your Max / Pro subscription

## Install

```bash
# install mngr (if not already installed)
curl -fsSL https://raw.githubusercontent.com/imbue-ai/mngr/main/scripts/install.sh | bash

# either select the robinhood plugin while installing above, or run:
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

The `mngr robinhood` command takes the same arguments as the regular
`claude` CLI, always behaves as if `-p`/`--print` was passed, and routes
the prompt through a fresh, ephemeral `mngr` claude agent. The agent runs
in-place in the current directory, processes the prompt (or stream of
prompts), and is destroyed when the command exits.

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

## tmux window size

The streamed response is reverse-mapped from the spawned agent's rendered tmux
pane, so the pane width determines where lines hard-wrap in the output. `mngr
robinhood` therefore creates its agent in a large, pinned window by default
(`2048` columns x `256` rows, resize policy `manual`) so the streamed text is not
chopped at a narrow width. Override with:

- `--tmux-width <columns>` (default `2048`)
- `--tmux-height <rows>` (default `256`)
- `--tmux-window-size manual|latest|largest|smallest` (default `manual`; `manual`
  pins the window to the given size and never resizes it)

These flags are consumed by the wrapper and not forwarded to the spawned claude;
an invalid value exits with code 2.

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

## Alternatives

`robinhood` is Claude-specific. For a generic alternative across agents, use [mngr](https://github.com/imbue-ai/mngr) directly: `mngr create --message "some prompt"` creates an agent and sends it a prompt; use `mngr transcript` to see the output or `mngr wait` to wait for it to finish.

# mngr-backed Agent SDK (experimental)

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
- `include_partial_messages` -> `StreamEvent` — when set, the agent's tmux pane is watched and the
  reconstructed assistant text is wrapped as the claude-native partial-event sequence
  (`message_start` -> `content_block_delta`(`text_delta`)* -> `message_stop`). Approximate (text is
  reconstructed from the rendered pane, not claude's token-level deltas) and best-effort; the
  authoritative `AssistantMessage` still arrives from the transcript, and `usage`/`total_cost_usd`
  remain on the final `ResultMessage`.

## Limitations

- `fork_session` raises `AgentSdkNotImplementedError`: claude does not assign a new session id when
  forking over an adopted, resumed interactive session, so a faithful fork cannot be produced on
  this transport.
- The agent is not hermetic from the host's claude config -- it must load real settings to
  authenticate -- whereas the real SDK with `setting_sources=[]` is hermetic.

Why so many limitations?

Honestly the Claude Agent SDK is kind of a mess,
and I don't have a particular reason to make it work perfectly.
There will always be some trade-offs--you'll have to decide how, exactly, to make them for your own use case.
Consider this implementation more of an example of how you could do it, or a starting point, rather than a finished product.

Instead of using the proprietary Claude Agent SDK interface, you should try using [mngr](https://github.com/imbue-ai/mngr) instead--
its API is considerably cleaner, and it abstracts over a variety of different coding agents already.
