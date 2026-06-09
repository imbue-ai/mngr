Extended `specs/agent-plugin-parity/spec.md` (dimension D, "subagent-aware idle gating")
with a note on a related premature-idle failure mode: the RUNNING/WAITING marker tracks the
agent's turn/loop, not work it detaches from that loop. Documents how a CLI's
`run_in_background`-style tool (or a `cmd &`) can make the agent report WAITING while a
launched task still runs; that claude does not solve this for backgrounded bash (its Stop
hook waits only for sibling stop-hook processes and *excludes* `CLAUDECODE=1` bash-tool
tasks); and that the `CLAUDECODE=1` tag is nonetheless the discriminator that *would* make a
descendant-liveness wait safe. Distinguishes in-loop pending work, which the CLIs' idle
signals do gate correctly (agy's `fullyIdle:true`-plus-root-match clears only on the root's
final Stop, not interim Stops or a subagent's own idle; pi's foreground tools block the turn
so `agent_end` waits for them), from detached work, which is loop-scoped for claude, agy, and
pi alike. Adds a matching investigation-checklist question.
