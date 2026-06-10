Merged the `pi-coding` and `opencode` agent-plugin ports into a single branch and
began unifying their cross-cutting pieces. Updated the agent-plugin-parity spec
(`specs/agent-plugin-parity/spec.md`) to reflect `mngr_opencode` as a real,
fully-implemented port rather than a `BaseAgent` stub: filled its column in the
capability matrix, added the HTTP client/server architecture as a fourth
integration lever alongside shell-hooks and the in-process extension, and
documented its real mechanisms across the parity dimensions.
