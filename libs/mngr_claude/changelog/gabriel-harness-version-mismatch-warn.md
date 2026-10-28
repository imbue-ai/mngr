New `version_mismatch` setting on the claude agent type: `ERROR` (the default, unchanged behavior) fails `mngr create` when the installed claude does not report the pinned `version`; `WARN` logs a warning and creates the agent on the installed binary. This lets a workspace whose claude has drifted from its template's pin still create the chat that would update it.

Behavior change: the pinned-version check now runs whenever `version` is set, even with `check_installation = false`, so `agent_types.claude.check_installation=false` no longer skips the version check (it still skips the install-if-missing step and the missing-credentials warning); use `version_mismatch = "WARN"` to keep a mismatch from failing the create.

claude's version check now uses the same shared verifier as codex, pi, and opencode: the pin is matched as a token of the `claude --version` banner rather than compared to its first word, and a probe that produces no output is skipped instead of reported as a mismatch.
