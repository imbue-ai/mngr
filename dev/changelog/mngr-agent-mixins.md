Added a design doc (`specs/agent-plugin-parity/capability-mixins.md`) proposing a code-derived agent capability taxonomy: capability mixins plus a registry that generates the parity matrix from the agent classes, replacing the hand-maintained table and guarding against doc/code drift.

Added `scripts/make_agent_capabilities_doc.py`, the dev-only generator for the code-derived agent capability matrix doc. It loads every installed mngr plugin (local backend only, so no docker/modal SDKs), builds the matrix from the agent classes + their plugins, and either rewrites `libs/mngr/docs/concepts/agent_capabilities.md` or, with `--check`, fails if it is stale. This mirrors `scripts/make_cli_docs.py` and keeps the generator out of the shipped `mngr` wheel (it has no runtime importers); the capability mixins it detects remain in `imbue.mngr.interfaces.agent`. The registry/detection logic and its tests moved here from the package (`scripts/make_agent_capabilities_doc_test.py`).

Added a `just regenerate-agent-capabilities-doc` recipe that runs the generator (`uv run python scripts/make_agent_capabilities_doc.py`).

Removed the throwaway synthetic-base doc (`dev/agent-mixins-synthetic-base.md`); the synthetic base branch is no longer needed.

Updated the capability-mixins design doc to match what shipped: the three-state `Y`/`-`/`n/a` matrix with the code-derived `CapabilityScope` model, the positive `CliBackedAgentMixin` kind marker, the unified `live_output` capability, and the `session_resume` capability (the original doc forbade `n/a` entirely).
