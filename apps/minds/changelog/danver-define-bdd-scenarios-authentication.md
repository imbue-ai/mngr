Added `apps/minds/specs/authentication.md`, the first proof-of-concept behavioral specification file: Gherkin scenarios plus cross-cutting invariants for the desktop client's sign-in (one-time login codes), session (signed cookie lifetime and integrity), landing-page routing, post-sign-in destination, and the one-sign-in-opens-every-workspace bridge.

The spec includes a traceability appendix mapping each scenario tag and invariant to the existing tests that verify it (marking partial coverage and gaps, e.g. session expiry has no covering test).

Fixed documentation drift surfaced while writing the spec: `desktop_client/README.md` claimed the session cookie was issued with `Domain=localhost` and that the landing page redirected straight to a sole agent (the cookie is host-only with subdomain access via the forward server's `/goto/` auth bridge, and the landing page lists workspaces even when there is exactly one); a test-helper docstring repeating the stale cookie claim was also corrected.

No runtime behavior changes.
