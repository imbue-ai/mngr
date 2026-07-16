Added `apps/minds/specs/authentication.md`, the first proof-of-concept behavioral specification file: Gherkin scenarios plus cross-cutting invariants for the desktop client's sign-in (one-time login codes), session (signed cookie lifetime and integrity), landing-page routing, post-sign-in destination, and the one-sign-in-opens-every-workspace bridge.

The spec includes a traceability appendix mapping each scenario tag and invariant to the existing tests that verify it (marking partial coverage and gaps), and records two documentation divergences found while writing it (the stale `Domain=localhost` cookie claim and the single-agent landing redirect described in `desktop_client/README.md`).

This is documentation only; no runtime behavior changes.
