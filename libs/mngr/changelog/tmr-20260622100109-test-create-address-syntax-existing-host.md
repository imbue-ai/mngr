Strengthened the e2e coverage for the `mngr create my-task@host` address syntax.

The existing `test_create_address_syntax_existing_host` (which targets a non-existent host and expects a clean failure) now also asserts that the failed create leaves no orphan agent behind, rather than only checking the error message.

Added a companion happy-path test, `test_create_address_syntax_targets_existing_host`, that creates a local agent, discovers its host, and then targets that existing host via the `name@host` address syntax -- verifying the second agent actually lands on the same existing host.
