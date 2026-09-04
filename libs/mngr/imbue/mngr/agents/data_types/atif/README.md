# Vendored ATIF models

This directory is a vendored copy of Harbor's pydantic models for the
**Agent Trajectory Interchange Format (ATIF)**. They define the target schema for the
common transcript (see `specs/atif-transcript-alignment/spec.md`): the stream-record
schema composes the sub-models (`ToolCall`, `Metrics`, `ObservationResult`), and the
doc-builder validates assembled documents against `Trajectory`.

## Provenance

- Source repo: <https://github.com/harbor-framework/harbor>
- Source path: `src/harbor/models/trajectories/`
- Vendored from: tag `v0.22.0`, commit `4407eb5227a2ff4f0d3f16b2eb48849382fdf276`
- Pinned schema version: `ATIF-v1.7` (the RFC lives at `rfcs/0001-trajectory-format.md`
  in the harbor repo)

Harbor cannot be a workspace dependency (its dependency floors do not co-resolve with
the workspace), so these ~10 files are vendored instead. They are self-contained: the
only imports are the stdlib, pydantic, and each other.

## Local deviations from upstream

Kept deliberately minimal so a future re-vendor is a fresh copy plus this same short
list:

- Intra-package imports rewritten from `harbor.models.trajectories.*` to
  `imbue.mngr.agents.data_types.atif.*`.
- `__init__.py` is empty (repo convention); import the models from their submodules
  directly (e.g. `from imbue.mngr.agents.data_types.atif.trajectory import Trajectory`).
- `Step.validate_timestamp` raises with `from e` (exception chaining; behavior
  otherwise identical).
- Formatting is normalized by this repo's ruff configuration (import sorting, line
  wrapping); no semantic changes.

The files otherwise intentionally keep upstream's style, so the vendored modules are
carved out of the six ratchets their patterns would trip: built-in `ValueError` raises
inside pydantic validators, data-driven `getattr` loops over field-name lists,
multi-option `Literal` fields, `Args:` docstring sections, `Returns:` docstring
sections, and an else-less `if`/`elif` chain. Upstream's module docstrings also diverge
from this repo's style guide, but no ratchet enforces that. Schema fidelity to upstream
beats local style here.

## Re-vendoring

Re-vendoring is a deliberate, manual act (there is no sync automation), done only when
we choose to adopt a newer ATIF revision:

1. Copy `src/harbor/models/trajectories/*.py` from the desired harbor commit over this
   directory (excluding `__init__.py`).
2. Re-apply the deviations listed above.
3. Update the provenance section (tag, commit, schema version) and the pinned
   `schema_version` used by the emitters and doc-builder.
4. Run the tests in this directory; update them for any schema changes.
