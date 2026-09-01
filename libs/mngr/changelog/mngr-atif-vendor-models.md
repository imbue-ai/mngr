Vendored harbor's ATIF (Agent Trajectory Interchange Format) pydantic models into `imbue/mngr/agents/data_types/atif/` (from harbor v0.21.0, pinned to ATIF-v1.7), as the foundation for aligning the common transcript with the ATIF standard (`specs/atif-transcript-alignment/spec.md`).

The vendored directory keeps upstream's style (documented in its README.md along with provenance and re-vendoring instructions) and is carved out of the ratchet checks its patterns would otherwise trip.
