Strengthened the `mngr config set headless true` tutorial e2e test
(`test_config_set_headless_globally`) to verify the value actually persists to
the project-scope config file (read back directly as a boolean), rather than
only checking the command exits 0. Added a companion unhappy-path test
(`test_config_set_rejects_unknown_key`) that confirms `mngr config set` rejects
an unknown key with a non-zero exit and does not create the config file. No
production behavior change.
