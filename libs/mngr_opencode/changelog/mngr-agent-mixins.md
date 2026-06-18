The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (per-resource permission policy via the `permission` config-override key).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `opencode` CLI is installed and installs it (`curl -fsSL https://opencode.ai/install | bash`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. A new `check_installation` config field (default `True`) disables the check when set to `False`.

The auto-allow permission apply-path (the wildcard `permission` config) now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`OpenCodeAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.

`OpenCodeAgent` now also declares `InteractiveAgentMixin` -- the marker for agents that accept interactive messages, now that `send_message` is no longer a universal `AgentInterface` method. OpenCode already implemented `send_message` (it POSTs to its server), so this only adds the marker. Behavior is unchanged.
