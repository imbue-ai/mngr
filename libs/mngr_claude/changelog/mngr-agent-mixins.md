`ClaudeAgent` now declares the `HasStreamingSnapshotMixin` capability mixin (it already implemented `get_stream_buffer_path`), so the live in-progress response-streaming view is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact.

`ClaudeAgent` also declares the `HasUnattendedModeMixin` capability (`is_unattended_enabled` reports the `auto_allow_permissions` config).
