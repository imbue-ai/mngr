Fixed the `test_destroy_all_modal_agents` release e2e test. Its before/after
verification listings now use `mngr list --provider modal` (scoping to the modal
provider) instead of `mngr list --include 'host.provider == "modal"'`, so an
enabled-but-unreachable provider (e.g. aws with no credentials) no longer makes
the verification listing exit non-zero even though the modal listing is correct.
The remote destroy pipeline and listings also now use the remote timeout rather
than the short default, so the real Modal teardown has time to complete.
