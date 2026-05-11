Add the ability to interrupt an agent's current turn without terminating it.

- New `InterruptibleAgentMixin` on `AgentInterface`: agent types that support aborting an in-progress turn implement `interrupt_current_turn()` and stay alive afterwards.
- New parallel-fanout API `interrupt_agents(...)` in `imbue.mngr.api.interrupt`, modeled on `send_message_to_agents`: resolves hosts and interrupts matching agents concurrently, reporting per-agent success/failure (including agents whose type does not implement the mixin).
- `ClaudeAgent` now implements `InterruptibleAgentMixin`: while the agent is `RUNNING`, the interrupt delivers Ctrl-C to its tmux pane (Claude Code treats Ctrl-C as "abort turn, keep session alive"); idle states are a no-op.
- New `InterruptAgentError` for surfacing tmux-level failures during interrupt.
