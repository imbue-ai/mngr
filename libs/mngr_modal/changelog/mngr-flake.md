Fix flakiness in `test_host_volume_is_symlinked_and_persists_data`.

The test asserted `get_volume_for_host(host) is not None` exactly once,
immediately after `create_host`. That probe does a fresh
`volume_from_name(create_if_missing=False)` followed by a `listdir("/")` to
confirm the volume exists. Right after sandbox creation, Modal's control plane
can take a moment to make the just-created volume resolvable by name, so the
probe transiently raises a volume-level `NotFoundError` (which is not retried by
`_should_retry_volume_op`) and `get_volume_for_host` returns `None`. The volume
genuinely exists -- the sandbox mounted it and the test wrote through it -- so
this was pure eventual-consistency lag.

The assertion now polls with `wait_for` (mirroring the sibling test
`test_host_volume_data_readable_via_volume_interface`) so the transient `None`
resolves within the timeout. No production code changed: real callers of
`get_volume_for_host` only run against long-lived hosts, never within a second
of creation.

The sibling test `test_host_volume_data_readable_via_volume_interface` had the
same single-shot probe of `get_volume_for_host` right after `create_host`; it
is now polled the same way to close the identical latent race.
