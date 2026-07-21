Audited the existing minds test suite against the `apps/minds/specs/authentication`
behavioral-spec corpus and added honest `@pytest.mark.witnesses(...)` markers to the
tests that already verify each unit. Coverage per `mngr specs matrix --root apps/minds/specs`
moved from 0 witnessed units to 5 full / 17 partial / 10 none, with no test behavior changed.

Then hand-wrote three witnessing tests to close the clearest gaps: an end-to-end
`fresh-code` sign-in flow, a scriptless-fetch test for `prefetch` / `fetch-never-spends`,
and a session-survives-restart test. Coverage is now 8 full / 17 partial / 7 none (the
7 remaining are the six `mngr_forward`-implemented bridge units, out of scope for the
`apps/minds` test tree, plus the time-dependent `expired-token`).
