Strengthened the `mngr plugin enable --scope project` tutorial e2e test: it now also asserts that the printed settings path is the project-scope `settings.toml` (not the local-scope `settings.local.toml`), confirming the scope routed correctly.

Added a new unhappy-path test verifying that `mngr plugin enable --scope <bogus>` is rejected with a click usage error (exit code 2, "Invalid value") and does not persist the unknown scope.
