Fixed a regression where the first `mngr create --provider modal` for a brand-new
Modal environment failed with "Provider 'modal' has no state yet" instead of
bootstrapping the environment. The create flow now resolves the new-host provider
with `is_for_host_creation=True`, allowing the per-user Modal environment to be
created on first use.

Strengthened the `test_create_modal_idle_mode_run` e2e test to verify the
concrete effect of the create (a command-type agent that runs the requested
command with run idle mode and a 60s timeout) instead of only checking the exit
code, and marked it flaky so offload retries transient remote Modal create
failures.
