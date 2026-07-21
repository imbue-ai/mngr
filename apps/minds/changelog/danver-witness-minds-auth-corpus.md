Audited the existing minds test suite against the `apps/minds/specs/authentication`
behavioral-spec corpus and added honest `@pytest.mark.witnesses(...)` markers to the
tests that already verify each unit. Coverage per `mngr specs matrix --root apps/minds/specs`
moved from 0 witnessed units to 5 full / 17 partial / 10 none, with no test behavior changed.
