Extended `specs/agent-plugin-parity/spec.md` (dimension D, "subagent-aware idle gating")
with a note on a related premature-idle failure mode: the RUNNING/WAITING marker tracks the
agent's conversational turn, not detached/background work. Documents how a CLI's
`run_in_background`-style tool can make the agent report WAITING while a launched task still
runs, how claude avoids it (its Stop hook waits for tagged `CLAUDECODE=1` descendant
bash-tool processes), why a CLI without such a tag can't safely do a generic
descendant-liveness check, and the turn-scoped fallback (agy/pi). Adds a matching
investigation-checklist question.
