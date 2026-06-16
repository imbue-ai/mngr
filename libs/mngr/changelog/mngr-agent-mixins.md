Added the agent capability registry (`imbue.mngr.agents.agent_capabilities`): a code-derived description of which agent types have which capabilities. Each capability declares how its presence is detected (a class mixin via `issubclass`, a `waiting_reason` field generator, a plugin hookimpl, or a sibling usage plugin).

`build_agent_class_infos` introspects a loaded plugin manager to determine each registered agent type's capabilities, and a generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the full capability matrix. A drift-guard test fails if the committed doc disagrees with the code; regenerate it with `just regenerate-agent-capabilities-doc`. This is the basis for replacing the hand-maintained parity matrix.

Added contract-bearing capability mixins to `imbue.mngr.interfaces.agent`: `HasStreamingSnapshotMixin`, `HasSessionPreservationMixin`, and `HasUnattendedModeMixin`. Agent types declare these to make the corresponding capabilities (live response streaming, session preservation on destroy, unattended/auto-allow operation) code-detectable in the capability matrix.

Added `HasPermissionPolicyMixin` (per-resource allow/deny/ask policy) and `HasVersionManagementMixin` (version pin or update policy) capability mixins.

Added module-level capabilities to the matrix: `deploy_contributions` (the `get_files_for_deploy` hookimpl) and `usage_tracking` (a sibling `mngr_<harness>_usage` plugin), both detected by the agent's owning plugin entry-point name.

Made auto-install a base capability: added `HasAutoInstallMixin` (per-CLI `get_install_command`) and a shared `ensure_cli_installed` helper that checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). All five agents now declare it; antigravity, opencode, and codex gain auto-install they previously lacked. Adds the `auto_install` row to the capability matrix and a new `AgentInstallationError`.

Verified opencode and antigravity auto-install end-to-end on real Modal hosts (which ship without the CLIs).

Architecture-review refinements: excluded the task-specialized skill variants (code-guardian, fixme-fairy) from the matrix (kept headless_claude, which runs genuinely different logic); added a dedicated `get_install_binary_name()` to `HasAutoInstallMixin` (decoupling the install check from the lifecycle-detection process name); and a construction-time validator on `AgentCapability` for the detection-kind/field invariant. The registry-driven behavioral exercise of each capability against a live agent is deferred to a follow-up release-test harness; detection is covered in CI by the drift guard and the builder integration test.

Gave the capability matrix a fixed column order (claude, headless_claude, antigravity, codex, opencode, pi-coding, command, headless_command) instead of alphabetical, and excluded the internal `mngr-proxy-child` agent. Rendering now raises if a registered agent type is neither in the fixed order nor the exclusion list, so a newly added agent can never be silently dropped from the table. Moved the two headless-output rows to the bottom of the matrix.

Added a third matrix state, `n/a`, for capabilities that do not apply to an agent kind (distinct from `-`, which means applicable but absent). Each capability now declares a code-derived scope based on the agent's kind:

- CLI-backed-only (`raw_transcript`, `common_transcript`, `auto_install`, `permission_policy`, `version_management`, `usage_tracking`): `n/a` for the bare command runners.

- Interactive-only (`waiting_reason_field`): `n/a` for headless and bare-command agents.

- TUI-driven-only (`streaming_snapshot`): `n/a` for everything except the keystroke-driven TUI agents (claude, codex, antigravity), since the snapshot works by scraping the rendered pane -- server/extension-driven agents (opencode, pi) get the same information from their API, and headless agents have no pane.

A genuinely-registered capability (field generator, usage source, deploy hook) that lands out of scope raises, keeping the matrix honest; an inherited capability mixin that lands out of scope (e.g. a headless variant of a TUI agent inheriting the snapshot mixin) just renders `n/a`.

Introduced `GenericCommandAgentMixin` to mark the bare command-running agent types (`command`, `headless_command`); it records that they are not CLI-backed and makes them inherently unattended (so `unattended_operation` now shows as present for them). The `command` type is now registered as a small `CommandAgent` subclass of `BaseAgent` carrying this marker.

Extracted `InteractiveTuiMixin`, a bare marker for TUI-driven agents (mngr sends keystrokes into the rendered pane). `InteractiveTuiAgent` now inherits it (unchanged otherwise), and `HasStreamingSnapshotMixin` subclasses it so the streaming-snapshot capability is, by construction, only declarable on a TUI-driven agent.
