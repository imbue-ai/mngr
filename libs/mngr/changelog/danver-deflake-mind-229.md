Deflake the list-agents continue-mode provider-error tests (MIND-229), which
intermittently turned CI red with `assert 2 == 1` and `assert False = all(...)`.
The three tests
(`test_list_agents_streaming_continue_mode_records_failing_provider_error`,
`test_list_agents_batch_continue_mode_records_failing_provider_error`, and
`test_list_agents_continue_mode_records_unauthenticated_provider_with_structured_fields`)
call `list_agents` in full-enumeration mode, which constructs and discovers over
*every* registered backend -- not just the intended `local` provider plus the one
broken/unauthenticated provider under test.

The test environment also registers the `lima` backend (it is deliberately kept
out of the remote-backend exclusion so it can be registered without `limactl`).
Its default instance's discovery shells out to `limactl` and reads the real
`/mngr` host directory, so whether it errors depends on the ambient CI
environment (limactl presence, `/mngr` contents, subprocess timing under load).
An incidental discovery failure there added an unexpected second `ProviderErrorInfo`
-- breaking the "exactly one provider error" and "all provider errors are
inaccessible" assertions non-deterministically.

The fix makes the tests hermetic: the ctx builders now pin `enabled_backends` to
just `local` plus the backend under test, so a full-enumeration listing only
touches those providers regardless of what else is registered. A new regression
test (`test_list_agents_continue_mode_ignores_incidental_backends`) registers an
extra erroring backend and asserts it never contaminates the listing, so removing
the scoping fails deterministically in CI rather than silently re-flaking. This is
a test-only change; production `list_agents` behavior is unchanged.
