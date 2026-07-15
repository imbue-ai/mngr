Fixed the isolated-venv install fixture to also editable-install the `overlay`
workspace package, which `mngr` now depends on. Without it, the freshly built
venv could not import `imbue.overlay` and every `minimal_install_env`-based
release test failed at startup.

Tightened the non-strict unknown-config-key release test to verify its scope
directly: it now checks (via a provider-independent `config get` command) that an
unknown config key is warned about but does not abort config loading, instead of
depending on a full `mngr list` run that also required a reachable provider
backend.
