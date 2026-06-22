Made the `test_create_modal_idle_mode_ssh` tutorial e2e test robust by scoping its verification `mngr list` to `--provider modal`.

Previously the test ran an unscoped `mngr list --format json`, which triggers discovery across every configured provider. In environments where another provider (e.g. AWS) lacks credentials, that discovery fails and `mngr list` exits non-zero, failing the test even though `mngr create --provider modal` succeeded. Scoping the listing to the Modal provider (matching the sibling idle-mode tests) keeps the verification reliable.
