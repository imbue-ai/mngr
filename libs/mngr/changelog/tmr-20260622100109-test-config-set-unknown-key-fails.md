Fixed the `test_config_set_unknown_key_fails` e2e tutorial test. The test
incorrectly assumed the e2e fixture pre-seeds the project-scope `settings.toml`,
but the fixture deliberately leaves it unseeded; a rejected `config set`
validates before writing and never creates the file, so the read-back failed.
The test now reads the project settings file back tolerantly and asserts the
rejected key was never persisted.

Also strengthened the assertion on the rejection error to require that it names
the specific offending key (`totally_unknown_key`), so the test cannot pass for
an unrelated "Unknown configuration fields" failure.
