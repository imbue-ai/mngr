The agent now declares the `HasSessionPreservationMixin` capability mixin: its `on_destroy` session-preservation step was extracted into a `preserve_session_state` method, so preserving session/transcript files on destroy is a code-detectable capability in the agent capability matrix rather than a hand-tracked fact. Behavior is unchanged.

`PiCodingAgent` also declares the `HasUnattendedModeMixin` capability. pi has no tool-approval gate, so it gains an `auto_allow_permissions` config field pinned to True (setting it False is rejected, since pi cannot enforce a deny) -- making "runs unattended" code-detectable and uniform with the other agents.

`PiCodingAgent` now exposes a `waiting_reason` agent field (the `agent_field_generators` hook). pi has no tool-approval gate, so the reason is single-valued (END_OF_TURN when idle), but wiring it through the shared classifier makes it a real extension point and a code-detectable capability.
