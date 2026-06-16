Added a design doc (`specs/agent-plugin-parity/capability-mixins.md`) proposing a code-derived agent capability taxonomy: capability mixins plus a registry that generates the parity matrix from the agent classes, replacing the hand-maintained table and guarding against doc/code drift.

Added a `just regenerate-agent-capabilities-doc` recipe that regenerates the code-derived agent capability matrix doc.

Removed the throwaway synthetic-base doc (`dev/agent-mixins-synthetic-base.md`); the synthetic base branch is no longer needed.

Updated the capability-mixins design doc to match what shipped: the three-state `Y`/`-`/`n/a` matrix with the code-derived `CapabilityScope` model, the positive `CliBackedAgentMixin` kind marker, the unified `live_output` capability, and the `session_resume` capability (the original doc forbade `n/a` entirely).
