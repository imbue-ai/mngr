The minds TMR mapper prompt (`apps/minds/tmr/mapper.j2`) is updated in step with the packaged mngr one, which changed how test agents report their results.

Agents no longer signal "this needs wider attention" by degrading a change's status to `BLOCKED`. That status is removed; agents now report an `escalations` list that is independent of their own outcome, so a test that passes cleanly can still flag a problem. Escalations come in two kinds: `BLOCKER` (the agent could not proceed without a shared change -- the common minds case of an absent Docker daemon, snapshot, deployed env, or secret) and `SHARED_PATTERN` (the agent's local fix worked, but sibling tests already carry it, meaning one shared change should replace them all).

This was a required change, not a cosmetic one: the minds prompt is a self-contained copy rather than an extension of the packaged template, and had it kept emitting `BLOCKED` its agents' outcomes would no longer parse, silently dropping every minds mapper's result from the run's report.

Minds test agents also no longer write changelog entries -- the run's integrator writes one unified entry -- and are told not to adjust per-test timeout markers, since the run supplies an explicit timeout that the integrator verifies at.
