Fixed the `test_plugin_disable_user_scope` e2e tutorial test to match the actual
behavior of `mngr plugin disable my-plugin --scope user`: disabling a not-yet-installed
plugin is a soft operation that succeeds (with a warning) and persists the setting. The
test now asserts the command succeeds, emits the "not currently registered" warning, and
verifies the disabled state is persisted by reading it back via `mngr config get
plugins.my-plugin.enabled --scope user`.
