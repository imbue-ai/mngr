Added the agent capability registry (`imbue.mngr.agents.agent_capabilities`): a code-derived description of which agent types have which capabilities. Each capability declares how its presence is detected (a class mixin via `issubclass`, a `waiting_reason` field generator, a plugin hookimpl, or a sibling usage plugin).

`build_agent_class_infos` introspects a loaded plugin manager to determine each registered agent type's capabilities, and a generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the full capability matrix. A drift-guard test fails if the committed doc disagrees with the code; regenerate it with `just regenerate-agent-capabilities-doc`. This is the basis for replacing the hand-maintained parity matrix.

Added contract-bearing capability mixins to `imbue.mngr.interfaces.agent`: `HasStreamingSnapshotMixin`, `HasSessionPreservationMixin`, and `HasUnattendedModeMixin`. Agent types declare these to make the corresponding capabilities (live response streaming, session preservation on destroy, unattended/auto-allow operation) code-detectable in the capability matrix.

Added `HasPermissionPolicyMixin` (per-resource allow/deny/ask policy) and `HasVersionManagementMixin` (version pin or update policy) capability mixins.

Added module-level capabilities to the matrix: `deploy_contributions` (the `get_files_for_deploy` hookimpl) and `usage_tracking` (a sibling `mngr_<harness>_usage` plugin), both detected by the agent's owning plugin entry-point name.

Made auto-install a base capability: added `HasAutoInstallMixin` (per-CLI `get_install_command`) and a shared `ensure_cli_installed` helper that checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). All five agents now declare it; antigravity, opencode, and codex gain auto-install they previously lacked. Adds the `auto_install` row to the capability matrix and a new `AgentInstallationError`.

Added a capability-coverage forcing test: every capability in the registry must point at the test(s) that exercise it, so test coverage cannot silently lag the matrix. Verified opencode auto-install end-to-end on a real Modal host (which ships without the CLI).
