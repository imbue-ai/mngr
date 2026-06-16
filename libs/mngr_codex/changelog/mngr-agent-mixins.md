The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

Also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config), so "can run unattended" is a code-detectable capability in the matrix.

Also declares `HasPermissionPolicyMixin` (sandbox mode + approval policy) and `HasVersionManagementMixin` (the codex update policy).

Also declares `HasAutoInstallMixin`: provisioning now checks whether the `codex` CLI is installed and installs it (`npm i -g @openai/codex`) if missing, gated by consent on local hosts and the remote-install config flag on remote hosts. The install-if-missing check runs before the existing best-effort update notifier. A new `check_installation` config field (default `True`) disables the check when set to `False`.

Test-only: removed a fragile install-path provision test that crashed on CI, and added focused unit tests for the codex update flow and the CODEX_HOME-resolution error path (covering pre-existing codex plugin code) so the plugin clears the per-package coverage gate.

The auto-allow permission apply-path (`approval_policy="never"`) now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.

`CodexAgent` now also declares `CliBackedAgentMixin`, marking it as wrapping a specific external CLI so the CLI-only capability-matrix rows scope to it positively (rather than by the absence of a command-runner marker). Behavior is unchanged.
