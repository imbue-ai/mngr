# Agent capabilities

<!-- GENERATED FILE -- do not edit by hand.
     Regenerate with `just regenerate-agent-capabilities-doc` (see `scripts/make_agent_capabilities_doc.py`). -->

Which agent types implement which capabilities, **derived from the code** (the agent classes
and their plugins), not maintained by hand. `Y` means present; `-` means applicable but
absent; `n/a` means the capability does not apply to that agent kind (an interactive-only
capability on a headless agent, or a CLI-specific capability on a bare command runner). See
`specs/agent-plugin-parity/capability-mixins.md` for the design.

| Capability | claude | headless_claude | antigravity | codex | opencode | pi-coding | command | headless_command |
|---|---|---|---|---|---|---|---|---|
| raw_transcript | Y | Y | Y | Y | Y | Y | n/a | n/a |
| common_transcript | Y | Y | Y | Y | Y | Y | n/a | n/a |
| waiting_reason_field | Y | n/a | - | Y | Y | Y | n/a | n/a |
| live_output | Y | Y | - | - | - | - | - | Y |
| session_preservation | Y | Y | Y | Y | Y | Y | n/a | n/a |
| session_resume | Y | n/a | Y | Y | Y | Y | n/a | n/a |
| auto_install | Y | Y | Y | Y | Y | Y | n/a | n/a |
| unattended_operation | Y | Y | Y | Y | Y | Y | Y | Y |
| permission_policy | - | - | Y | Y | Y | - | n/a | n/a |
| version_management | Y | Y | - | Y | - | - | n/a | n/a |
| deploy_contributions | Y | - | - | - | - | - | - | - |
| usage_tracking | Y | - | - | Y | Y | Y | n/a | n/a |
| headless_output | n/a | Y | n/a | n/a | n/a | n/a | n/a | Y |

## Capabilities

- **raw_transcript** -- Copies the agent's native session JSONL verbatim into the agent state dir. Baseline; every port wants it.
- **common_transcript** -- Emits the agent-agnostic common transcript that `mngr transcript` renders. Baseline; every port wants it.
- **waiting_reason_field** -- Surfaces why a WAITING agent is blocked (PERMISSIONS vs END_OF_TURN) in `mngr list`. Wanted if the CLI prompts for tool approval.
- **live_output** -- Publishes a live, in-progress view of the agent's output before a turn completes -- a streaming snapshot of the rendered pane for TUI agents, or incremental stdout chunks for headless agents. Lowest-priority; only needed if a consuming UI wants live streaming.
- **session_preservation** -- Preserves session/transcript files when the agent is destroyed, so the conversation is not lost. Baseline; every port wants it.
- **session_resume** -- Adopts an existing conversation into a freshly created interactive agent (e.g. `--adopt-session <id>` or `--from <agent>` carry-forward), so it resumes prior context in a live session. The read-side counterpart to session_preservation; interactive-only, since a headless run has no live session to resume.
- **auto_install** -- Installs its CLI binary at provision time if missing (gated by consent locally, a config flag remotely). Baseline; every real agent wants it.
- **unattended_operation** -- Can complete a run with no human. Interactive coding agents earn this by auto-allowing in-run tool prompts; headless and bare-command agents have it by construction (no prompt to gate on). The load-bearing capability for remote / scheduled / headless agents.
- **permission_policy** -- Supports a per-resource allow/deny/ask permission policy (a refinement on plain auto-allow). Only some CLIs expose per-tool config.
- **version_management** -- Controls which version of its binary runs, by pinning a version or following an update policy. Absent for CLIs that just use whatever is on PATH.
- **deploy_contributions** -- Bakes config/cred files + env vars into a `mngr schedule` image (via the get_files_for_deploy hookimpl). Only needed if the agent runs under `mngr schedule`.
- **usage_tracking** -- Emits token/cost usage that `mngr usage` aggregates (via a sibling `mngr_<harness>_usage` plugin). Wanted so the agent's spend is visible.
- **headless_output** -- Runs non-interactively and exposes its output via output(). Only for headless agent variants.
