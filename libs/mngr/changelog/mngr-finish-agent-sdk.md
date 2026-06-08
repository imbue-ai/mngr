# Add a public `set_command` setter on agents

Agents now expose a certified `set_command(command)` setter (on `AgentInterface` / `BaseAgent`)
alongside the existing `get_command`, mirroring the other certified field getters/setters
(`set_labels`, `set_is_start_on_boot`, ...). It persists the agent's stored launch command through
the same atomic write + external-storage save path as the other setters. This lets callers update
the command that an agent re-runs on its next start/restart without reaching into the agent's
on-disk `data.json` directly.
