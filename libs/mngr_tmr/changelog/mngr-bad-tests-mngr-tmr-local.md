Improved test quality in the test-mapreduce plugin's unit tests:

- Fixed an order-dependent test-isolation bug: the report module's process-global
  outcome caches (`_TESTING_OUTCOME_CACHE` / `_INTEGRATOR_OUTCOME_CACHE`) are keyed
  only by agent name and ignore the output directory, so tests reusing an agent name
  across distinct temp dirs could read each other's cached outcomes. Added a
  `reset_outcome_caches()` helper and an autouse fixture that clears the caches
  between tests.
- Removed tautological model-construction tests and library-only tests (markdown
  passthrough) that could not catch any real regression.
- Rewrote the integrator-with-failures report test to assert on the rendered failure
  marker instead of merely that the output file exists, and gave it a unique agent
  name.
- Replaced enum-value smoke tests with parse-contract tests on the JSON wire format
  the agents actually rely on.
- Tightened `_merged_status_html` assertions to check the full HTML entities
  (`&#10003;` / `&#10007;`) and added coverage for the impl-priority-without-hash
  branch.
- Trimmed prompt and CLI help-text tests to assert only the values this plugin is
  responsible for (interpolated prompt values; that the shared option decorators
  stay wired onto `tmr`), rather than template prose or framework-owned behavior.
