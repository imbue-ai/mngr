Add the ability to interrupt an agent by stopping and restarting it.

- New parallel-fanout API `interrupt_agents(...)` in `imbue.mngr.api.interrupt`: resolves hosts and interrupts matching agents concurrently, reporting per-agent success/failure. Each agent's process is stopped (terminating any in-flight work and background tasks) and then restarted; agent types with session-resumption (e.g. Claude via `--resume`) pick up their saved state on restart. Configured `resume_message` is sent after restart, mirroring `mngr start`.
- Extracted `send_resume_message_if_configured` from `cli/start.py` into `imbue.mngr.api.start` so the interrupt API can reuse it without depending on CLI code.
