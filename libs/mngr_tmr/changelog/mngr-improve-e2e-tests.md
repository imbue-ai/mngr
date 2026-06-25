Reworked the TMR agent prompts so generated e2e tests converge to a stable size instead of accreting assertions every run:

- The testing-agent (mapper) prompt now anchors test quality to two sources: the claims the test's tutorial block explicitly makes (a hard requirement), and the effect each command or flag implies (a command must do something observable, and a flag must change something versus running without it -- the assertion should fail if the command were a no-op). Every assertion must trace back to one of those two sources; removing an over-fitted assertion that serves neither is a first-class improvement (recorded as `IMPROVE_TEST`) on par with adding one. Leaving an already-converged test unchanged is an explicitly correct outcome. The old "verify as thoroughly as possible" guidance, which pushed tests to grow without bound, is gone.

- Testing agents now flag cross-cutting setup that they cannot fix from a single test (e.g. mocking a claude/codex agent with `sleep`, a missing shared fixture, or absent credentials) with `# FIXME(tmr): ...` comments instead of papering over them with brittle local hacks.

- The integrator (reducer) prompt gained a normalize stage that runs on the integrated suite: it extracts genuinely-duplicated scaffolding into shared helpers -- but only steps that do NOT come from a test's tutorial block, preserving the 1:1 test/tutorial relationship -- and triages the `FIXME(tmr)` blockers, resolving the ones it can verify suite-wide and escalating the rest.

- The integrator outcome schema and the HTML report now carry `normalizations` (suite-wide cleanups applied) and `escalations` (blockers surfaced to the user), so unresolved cross-cutting issues are visible in the report rather than silently dropped.

- The reducer prompt now tells the integrator to verify changes by running the affected e2e tests directly with pytest (scoped to the blast radius they touched) in its own work_dir before publishing. Verified end to end on a trial run: a reducer integrated two mappers' assertion trims and re-ran the affected help tests to confirm them.
