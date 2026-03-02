The allowed import order for the modules in this directory is as follows (from highest level to lowest level).

This matches the import-linter contract in the root pyproject.toml:

- `desktop_client`
- `cli`
- `deployment`
- `forwarding_server`
- `core`
- `interfaces`
- `config`
- `errors`
- `data_types`
- `utils`
- `primitives`

Lower-level modules may not import from higher-level modules, but higher-level modules may import from lower-level modules.
This is to ensure a clear separation of concerns and to avoid circular dependencies.
