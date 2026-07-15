Scoped the `mngr list` call in the `--no-start-on-boot` Modal e2e test to `--provider modal` so it no longer fails when unrelated providers (e.g. AWS) lack credentials in the test environment.
