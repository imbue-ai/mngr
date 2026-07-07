Strengthened the mngr-tutor test suite. Replaced tautological constructor tests in
`data_types_test.py` with discriminated-union serialization round-trip tests; made the
tutor CLI test assert the lesson selector is invoked with `ALL_LESSONS` rather than only
checking the exit code; replaced weak `len(...) > 0` lesson assertions with structural
invariants (create-first/destroy-last, consistent agent name per lesson); and tightened
the TUI tests to assert the check alarm is scheduled at the correct interval with the
correct callback and that the refreshed frame body actually contains the current step.
Added integration tests (`test_checks.py`) covering the positive branches of every step
check against a real agent.
