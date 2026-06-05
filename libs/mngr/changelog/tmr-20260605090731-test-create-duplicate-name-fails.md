Strengthened the e2e test for duplicate agent names (`mngr create` with an
already-used name). It now asserts the failure is specifically the
duplicate-name rejection ("already exists") and verifies the rejected duplicate
leaves the original agent untouched -- exactly one agent of that name remains,
still running its original command rather than the duplicate's.
