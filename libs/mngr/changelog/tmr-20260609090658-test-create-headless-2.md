Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`. TOML
forbids duplicate keys, so every e2e tutorial test using this fixture failed with
"Cannot overwrite a value" while parsing the config. Removed the duplicate.

Also gave `test_create_headless` the same `@pytest.mark.timeout(120)` its sibling
multi-operation tests carry (it runs create + list + exec, exceeding the 10s
default), and strengthened its assertion to verify the headless agent actually
runs inside its dedicated worktree rather than only checking the `exec` exit code.
