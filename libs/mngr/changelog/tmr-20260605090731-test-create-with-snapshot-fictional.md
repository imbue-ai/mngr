Fixed a regression where `mngr create --provider modal` (and any other backend
with one-time bootstrap resources) failed against a brand-new Modal environment
with `Provider 'modal' has no state yet: Modal environment ... does not exist
yet`. The create path eagerly loaded the provider for failure-teardown using a
read-only construction (`is_for_host_creation=False`), which refused to
bootstrap the environment before the actual host-creation step could create it.
It now loads with `is_for_host_creation=True`, matching the create intent (the
instance is cached, so no second provider is built).

Also tightened the `test_create_with_snapshot_fictional` release test: it now
runs the full `mngr create --provider modal --snapshot snap-123abc` flow with an
explicit agent type, verifies the bad snapshot id is actually handed to Modal
and rejected, and asserts the failure is a clean single-line error with no raw
Python traceback.
