# Agent capabilities

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with `just regenerate-agent-capabilities-doc` (see `mngr.agents.agent_capabilities`). -->

Which agent types implement which capabilities, **derived from the code** (the agent classes
and their plugins), not maintained by hand. A `Y` means the capability is present; `-` means
absent. See `specs/agent-plugin-parity/capability-mixins.md` for the design.

| Capability | antigravity | claude | codex | command | headless_claude | headless_command | mngr-proxy-child | opencode | pi-coding |
|---|---|---|---|---|---|---|---|---|---|
| raw_transcript | Y | Y | Y | - | Y | - | Y | Y | Y |
| common_transcript | Y | Y | Y | - | Y | - | Y | Y | Y |
| headless_output | - | - | - | - | Y | Y | - | - | - |
| streaming_headless_output | - | - | - | - | Y | Y | - | - | - |
| waiting_reason_field | - | Y | Y | - | - | - | - | Y | Y |
| streaming_snapshot | - | Y | - | - | Y | - | Y | - | - |
| session_preservation | Y | Y | Y | - | Y | - | Y | Y | Y |
| auto_install | Y | Y | Y | - | Y | - | Y | Y | Y |
| unattended_operation | Y | Y | Y | - | Y | - | Y | Y | Y |
| permission_policy | Y | - | Y | - | - | - | - | Y | - |
| version_management | - | Y | Y | - | Y | - | Y | - | - |
| deploy_contributions | - | Y | - | - | - | - | - | - | - |
| usage_tracking | - | Y | Y | - | - | - | - | Y | Y |

## Capabilities

- **raw_transcript** -- Copies the agent's native session JSONL verbatim into the agent state dir. Baseline; every port wants it.
- **common_transcript** -- Emits the agent-agnostic common transcript that `mngr transcript` renders. Baseline; every port wants it.
- **headless_output** -- Runs non-interactively and exposes its output via output(). Only for headless agent variants.
- **streaming_headless_output** -- A headless agent that also streams output incrementally. Only for headless agent variants.
- **waiting_reason_field** -- Surfaces why a WAITING agent is blocked (PERMISSIONS vs END_OF_TURN) in `mngr list`. Wanted if the CLI prompts for tool approval.
- **streaming_snapshot** -- Publishes a live, in-progress view of the agent's assistant text. Lowest-priority; only needed if a consuming UI wants live streaming.
- **session_preservation** -- Preserves session/transcript files when the agent is destroyed, so the conversation is not lost. Baseline; every port wants it.
- **auto_install** -- Installs its CLI binary at provision time if missing (gated by consent locally, a config flag remotely). Baseline; every real agent wants it.
- **unattended_operation** -- Can complete a run with no human by auto-allowing in-run tool prompts. The load-bearing capability for remote / scheduled / headless agents.
- **permission_policy** -- Supports a per-resource allow/deny/ask permission policy (a refinement on plain auto-allow). Only some CLIs expose per-tool config.
- **version_management** -- Controls which version of its binary runs, by pinning a version or following an update policy. Absent for CLIs that just use whatever is on PATH.
- **deploy_contributions** -- Bakes config/cred files + env vars into a `mngr schedule` image (via the get_files_for_deploy hookimpl). Only needed if the agent runs under `mngr schedule`.
- **usage_tracking** -- Emits token/cost usage that `mngr usage` aggregates (via a sibling `mngr_<harness>_usage` plugin). Wanted so the agent's spend is visible.
