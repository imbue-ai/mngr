Extended `specs/agent-plugin-parity/spec.md` (dimension D, "subagent-aware idle gating")
with a note on a related premature-idle failure mode: the RUNNING/WAITING marker tracks the
agent's conversational turn, not detached/background work. Documents how a CLI's
`run_in_background`-style tool can make the agent report WAITING while a launched task still
runs, why claude doesn't currently solve it either (its Stop hook waits for sibling stop
hooks but excludes `CLAUDECODE=1` bash-tool tasks, so a background bash leaves its marker
turn-scoped too), why the `CLAUDECODE=1` tag is nonetheless the discriminator that *would*
make a descendant-liveness wait safe, and the turn-scoped fallback (agy/pi). Adds a matching
investigation-checklist question.
