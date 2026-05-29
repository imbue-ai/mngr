Strengthened the `mngr config path --scope` e2e tutorial test to verify the
reported path is the actual user-scope `settings.toml` config file (asserting it
is an absolute, existing file whose contents match the provisioned config),
rather than only checking the command exits successfully. Added a companion
unhappy-path test confirming an unknown `--scope` value is rejected.
