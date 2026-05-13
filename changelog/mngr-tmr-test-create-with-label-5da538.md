Add an explicit 120-second pytest timeout to `test_create_with_label` so that
the e2e test does not hit the project's default 10-second timeout when invoked
directly via plain `pytest` (matching the convention already used by other
Modal-using e2e tests in this directory).
