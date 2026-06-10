Merged the `pi-coding` and `opencode` agent-plugin ports into a single branch and
began unifying their cross-cutting pieces. Updated the agent-plugin-parity spec
(`specs/agent-plugin-parity/spec.md`) to reflect `mngr_opencode` as a real,
fully-implemented port rather than a `BaseAgent` stub: filled its column in the
capability matrix, added the HTTP client/server architecture as a fourth
integration lever alongside shell-hooks and the in-process extension, and
documented its real mechanisms across the parity dimensions.

Also updated the same spec to reflect `mngr_codex` as a real, fully-implemented
shell-hooks port rather than the lone `BaseAgent` stub: filled its column in the
capability matrix, and documented its real mechanisms across the parity
dimensions -- most notably its third, distinct subagent-aware idle-gating shape
(dedicated `SubagentStart`/`SubagentStop` hooks tracking one file per in-flight
async subagent, with the `active` marker recomputed under an `mkdir`-based lock).
No named agent type is a stub any more.
