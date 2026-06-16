`ClaudeAgent` now declares the `HasStreamingSnapshotMixin` capability mixin (it already implemented `get_stream_buffer_path`), so the live in-progress response-streaming view is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact.

`ClaudeAgent` also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config).

`ClaudeAgent` also declares `HasVersionManagementMixin` (version pin, else auto-update).

The auto-allow permission apply-path now reads through the `is_unattended_enabled()` contract instead of the `auto_allow_permissions` config field directly, making that method the single source of truth for unattended mode. Behavior is unchanged.
