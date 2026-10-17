pi now applies model/effort switches from the chat model bar. The bar's resolver writes a
switch intent to `<state>/pi_control.json (single-slot mailbox)` ({model_id, thinking_level}); the lifecycle
extension now watches that file and applies the newest intent natively -- resolving the
`provider/model` slug through pi's model registry and calling pi's own `setModel` /
`setThinkingLevel`. Applying fires pi's `model_select` / `thinking_level_select` events,
which already write `pi_model_state.json`, so the bar reconciles with no extra wiring.
Because pi is a single client that both applies and records the change, this is fully
consistent (no separate-client desync). A switch to an unauthenticated provider, or to a
model pi can't resolve, is logged and skipped.
