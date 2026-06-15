# Agent capabilities

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with `just regenerate-agent-capabilities-doc` (see `mngr.agents.agent_capabilities`). -->

Which agent types implement which capabilities, **derived from the code** (the agent classes
and their plugins), not maintained by hand. A `Y` means the capability is present; `-` means
absent. See `specs/agent-plugin-parity/capability-mixins.md` for the design.

| Capability | antigravity | claude | code-guardian | codex | command | fixme-fairy | headless_claude | headless_command | mngr-proxy-child | opencode | pi-coding |
|---|---|---|---|---|---|---|---|---|---|---|---|
| raw_transcript | Y | Y | Y | Y | - | Y | Y | - | Y | Y | Y |
| common_transcript | Y | Y | Y | Y | - | Y | Y | - | Y | Y | Y |
| headless_output | - | - | - | - | - | - | Y | Y | - | - | - |
| streaming_headless_output | - | - | - | - | - | - | Y | Y | - | - | - |
| waiting_reason_field | - | Y | - | Y | - | - | - | - | - | Y | - |
| streaming_snapshot | - | Y | Y | - | - | Y | Y | - | Y | - | - |

## Capabilities

- **raw_transcript** -- Copies the agent's native session JSONL verbatim into the agent state dir. Baseline; every port wants it.
- **common_transcript** -- Emits the agent-agnostic common transcript that `mngr transcript` renders. Baseline; every port wants it.
- **headless_output** -- Runs non-interactively and exposes its output via output(). Only for headless agent variants.
- **streaming_headless_output** -- A headless agent that also streams output incrementally. Only for headless agent variants.
- **waiting_reason_field** -- Surfaces why a WAITING agent is blocked (PERMISSIONS vs END_OF_TURN) in `mngr list`. Wanted if the CLI prompts for tool approval.
- **streaming_snapshot** -- Publishes a live, in-progress view of the agent's assistant text. Lowest-priority; only needed if a consuming UI wants live streaming.
