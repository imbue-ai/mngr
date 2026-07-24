Audited the existing minds test suite against the `apps/minds/specs/authentication`
behavioral-spec corpus and added honest `@pytest.mark.witnesses(...)` markers to the
tests that already verify each unit. Coverage per `mngr specs matrix --root apps/minds/specs`
moved from 0 witnessed units to 5 full / 17 partial / 10 none, with no test behavior changed.

Then hand-wrote three witnessing tests to close the clearest gaps: an end-to-end
`fresh-code` sign-in flow, a scriptless-fetch test for `prefetch` / `fetch-never-spends`,
and a session-survives-restart test.

Finally, hand-generated the rest locally (after the spec-anchored fleet could not run
in this environment): an `expired-token` HTTP test using a deterministically backdated
cookie (new `make_backdated_session_cookie` helper in `desktop_client/testing.py`, no
clock mocking), plus tightening `used-code`, `unknown-code`, `signed-out-home`,
`already-signed-in`, and `deep-link-prefill` from partial to full witnesses. Coverage is
now 14 full / 12 partial / 6 none per `mngr specs matrix --root apps/minds/specs`; the 6
remaining `none` are exactly the six `mngr_forward`-implemented bridge units, out of scope
for the `apps/minds` test tree.
