Hardened several over-broad or silent edge-case handlers in the subagent proxy:

- PostToolUse cleanup no longer destroys a subagent whose lifecycle state is UNKNOWN or RUNNING_UNKNOWN_AGENT_TYPE. It now uses a positive terminal allowlist ({DONE, STOPPED, REPLACED}); any other (or future) state is preserved, so a child that is transiently undiscoverable while still working is no longer torn down mid-task.
- Provisioning now fails loudly with a clear error when an agent's `.claude/settings.local.json` exists but contains malformed JSON (or a non-object), instead of crashing with a bare decode error in two code paths while three others silently degraded. The write paths abort rather than clobber the file.
- A malformed `MNGR_SUBAGENT_DEPTH` / `MNGR_MAX_SUBAGENT_DEPTH` value, or a transient transcript-stat failure feeding the permission gate, is now logged instead of silently swallowed.
