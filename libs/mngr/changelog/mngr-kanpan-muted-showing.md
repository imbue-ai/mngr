Test-only change: made `test_offline_agent_field_generators_hookspec_is_registered_and_collected`
robust to real plugins registering the `offline_agent_field_generators` hook.
It now asserts that the test plugin's entry is *among* the collected results
(rather than the sole result) and uses a test-specific plugin name, so it no
longer breaks when a real consumer (kanpan's `muted` field) also registers the
hook. No user-facing changes.
