Tighten the behavior witnesses in `server_test.py` so each traced assertion verifies exactly the clause it is marked for. These are the accepted results of the first `mngr witness` run over the `libs/mngr_forward/behaviors` corpus, reviewed adversarially by the pipeline's review node; the coverage matrix is unchanged at 54 full, 16 partial, 0 none over 70 units.

- `authentication.bridge-destination`: the goto-bridge witness now asserts that the redemption happens on the agent's origin, not only that it lands on the agent path.

- `authentication.signed-out-home`: the signed-out home page test also witnesses the which-agents-exist half of `no-data-without-session`, with a partial note naming what it does not cover.

- `no-data-without-session` and `credential-not-forwarded`: two new witnesses cover the agent-data facet of a signed-out bare-origin request and the WebSocket handshake surface (the backend handshake carries no session cookie).

- `forwarding.service-origin`: the service-origin routing test exercises both example host forms.

- `spent-code-refused`: the refusal assertion is relaxed from `== 403` to `>= 400`, since the step names no exact status.

- Two gold-plated markers removed: `forwarding.never-serves-host-loopback` no longer claims the operator opt-in test, whose assertion did not witness the clause, and `forwarding.default-service-redirect` no longer claims the non-HTML test, whose only assertion said nothing about the redirect.
