Add the forward plugin's behavioral-spec corpus at `libs/mngr_forward/specs/` -- the second corpus in the repo, following the language defined by the behavioral-specs skill and the `apps/minds/specs/` exemplar.

The corpus covers the proxy + auth core: an `authentication/` area (sign-in with a one-time code, session lifetime and integrity, the pre-authorized path for an embedding host, the bare-origin home page, and the goto bridge onto workspace origins) and a `forwarding/` area (host-header routing, HTTP and WebSocket byte-forwarding semantics, backend error behavior, and the host-loopback refusal invariant), plus corpus-root invariants (single-use codes, no data without a session, unforgeable sessions, the session cookie never reaching agent code, no open redirects).

The stdout envelope stream, discovery/resolution modes, reverse tunnels, and the CLI contract are deliberately deferred to future areas; the deferred envelope side channel is noted in `forwarding/backend-errors.md`.

Add a live-corpus guard test (`test_spec_corpus.py`) asserting the corpus always validates against the spec language, with `imbue-mngr-specs` as a new dev-group dependency. Annotating tests with `witnesses` markers is deferred, matching the minds precedent.
