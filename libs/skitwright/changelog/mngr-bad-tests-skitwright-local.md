Strengthened the skitwright test suite after a bad-tests review: added unit tests that
verify the runner keeps real subprocess stdout/stderr separate and records both streams
in `output_lines`, and that `Session` honors its `cwd`, `env`, and `comment` arguments
(previously passed but never asserted to take effect). Removed a tautological exit-code
assertion, made `expect()` dispatch tests assert the returned expectation type, and gave
the timeout test a unique sleep duration.
