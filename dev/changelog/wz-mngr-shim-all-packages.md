The `mngr` dev shim (`scripts/mngr`) now runs `uv run --all-packages`, so pulling a commit that adds a dependency to an mngr plugin no longer breaks the `mngr` command until you hand-run `uv sync --all-packages`.

The workspace root project does not depend on `imbue-mngr` or any of its plugin packages, so the shim's `uv run --project <root>` never considered them and never installed their dependencies. Because plugins are editable workspace installs, a plugin kept its registered entry point across a pull while a newly declared dependency of it stayed missing -- and since `mngr` imports every entry point at startup, that broke *every* subcommand, not just the plugin's own:

```
% mngr create my-agent
ModuleNotFoundError: No module named 'hypercorn'
```

`hypercorn` is a dependency of `imbue-mngr-forward` alone, so nothing about `mngr create` hints at why it is needed.

The shim now converges the venv on each invocation, so this resolves itself on the next `mngr` call. There is no measurable startup cost when the venv is already up to date.
