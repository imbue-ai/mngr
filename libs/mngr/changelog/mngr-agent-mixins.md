Added the agent capability registry (`imbue.mngr.agents.agent_capabilities`): a code-derived description of which agent types have which capabilities. Each capability declares how its presence is detected (a class mixin via `issubclass`, a `waiting_reason` field generator, a plugin hookimpl, or a sibling usage plugin).

`build_agent_class_infos` introspects a loaded plugin manager to determine each registered agent type's capabilities, and a generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the full capability matrix. A drift-guard test fails if the committed doc disagrees with the code; regenerate it with `just regenerate-agent-capabilities-doc`. This is the basis for replacing the hand-maintained parity matrix.

Added contract-bearing capability mixins to `imbue.mngr.interfaces.agent`: `HasStreamingSnapshotMixin`, `HasSessionPreservationMixin`, and `HasUnattendedModeMixin`. Agent types declare these to make the corresponding capabilities (live response streaming, session preservation on destroy, unattended/auto-allow operation) code-detectable in the capability matrix.

Added `HasPermissionPolicyMixin` (per-resource allow/deny/ask policy) and `HasVersionManagementMixin` (version pin or update policy) capability mixins.

Added module-level capabilities to the matrix: `deploy_contributions` (the `get_files_for_deploy` hookimpl) and `usage_tracking` (a sibling `mngr_<harness>_usage` plugin), both detected by the agent's owning plugin entry-point name.

Made auto-install a base capability: added `HasAutoInstallMixin` (per-CLI `get_install_command`) and a shared `ensure_cli_installed` helper that checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). All five agents now declare it; antigravity, opencode, and codex gain auto-install they previously lacked. Adds the `auto_install` row to the capability matrix and a new `AgentInstallationError`.

Verified opencode and antigravity auto-install end-to-end on real Modal hosts (which ship without the CLIs).

Architecture-review refinements: excluded the task-specialized skill variants (code-guardian, fixme-fairy) from the matrix (kept headless_claude, which runs genuinely different logic); added a dedicated `get_install_binary_name()` to `HasAutoInstallMixin` (decoupling the install check from the lifecycle-detection process name); and a construction-time validator on `AgentCapability` for the detection-kind/field invariant. The registry-driven behavioral exercise of each capability against a live agent is deferred to a follow-up release-test harness; detection is covered in CI by the drift guard and the builder integration test.

Gave the capability matrix a fixed column order (claude, headless_claude, antigravity, codex, opencode, pi-coding, command, headless_command) instead of alphabetical, and excluded the internal `mngr-proxy-child` agent. Rendering now raises if a registered agent type is neither in the fixed order nor the exclusion list, so a newly added agent can never be silently dropped from the table. Moved the `headless_output` row to the bottom of the matrix.

Added a third matrix state, `n/a`, for capabilities that do not apply to an agent kind (distinct from `-`, which means applicable but absent). Each capability now declares a code-derived scope based on the agent's kind:

- CLI-backed-only (`raw_transcript`, `common_transcript`, `auto_install`, `permission_policy`, `version_management`, `usage_tracking`): `n/a` for the bare command runners.

- Interactive-only (`waiting_reason_field`, `session_resume`): `n/a` for headless and bare-command agents.

- Headless-only (`headless_output`): `n/a` for every non-headless agent, since exposing `output()` non-interactively is meaningless for an interactive agent.

A genuinely-registered capability (field generator, usage source, deploy hook) that lands out of scope raises, keeping the matrix honest; an inherited capability mixin that lands out of scope just renders `n/a`.

CLI-backed scope is derived from a positive marker, `CliBackedAgentMixin`, inherited by every agent that wraps a specific external CLI (claude, codex, antigravity, opencode, pi, and headless variants). A bare command runner is simply the agent without that marker, so it needs no command-specific class for scoping; a minimal `CommandAgent` subclass of `BaseAgent` survives only to declare `HasUnattendedModeMixin`. `unattended_operation` shows present for every agent: interactive coding agents earn it by auto-allowing in-run tool prompts, while headless and bare-command agents have it by construction (no prompt to gate on), declared via `BaseHeadlessAgent` and `CommandAgent`.

Unified the TUI streaming snapshot and headless incremental output into a single `live_output` capability via a shared bare marker, `SupportsLiveOutputMixin`, inherited by both `HasStreamingSnapshotMixin` (the TUI agent's snapshot file) and `StreamingHeadlessAgentMixin` (a headless agent's incremental stdout). `headless_output` (plain `HeadlessAgentMixin`) remains a separate row.

Added a `session_resume` capability (the read-side counterpart to `session_preservation`) via `HasSessionAdoptionMixin`, whose `adopt_session` contract method an agent's `on_after_provisioning` calls to resume an existing conversation. Interactive-only: it resumes a live session, so it is `n/a` for headless and bare-command agents (e.g. `headless_claude` inherits the mixin from `ClaudeAgent` but is headless, so it renders `n/a`). Currently claude-only (its `--adopt-session` / `--from` carry-forward); other interactive CLI agents show it as an available-but-absent gap.
