The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (per-resource permission policy via the settings `permissions` block).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `agy` CLI is installed and installs it (`curl -fsSL https://antigravity.google/cli/install.sh | bash`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. A new `check_installation` config field (default `True`) disables the check when set to `False`.
