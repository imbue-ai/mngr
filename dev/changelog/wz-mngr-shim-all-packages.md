The `mngr` dev shim (`scripts/mngr`) now runs `uv run --all-packages`, so it keeps the workspace venv complete instead of assuming someone already ran `uv sync --all-packages`.

The workspace root project does not depend on `imbue-mngr` or any of its plugin packages, so a plain `uv run --project <root>` only guarantees the root's own dependencies. A venv synced without `--all-packages` produced two confusing failures:

- A plugin's dependency could be missing while its entry point remained registered, so `mngr` died at startup on an unrelated import (e.g. `ModuleNotFoundError: No module named 'hypercorn'`, a dependency of `imbue-mngr-forward` only) no matter which subcommand you ran.

- If `mngr` itself was missing from the venv, `uv run mngr` resolved `mngr` off `PATH`, found the shim, and re-executed it until uv aborted with "`uv run` was recursively invoked 101 times".

Both now self-heal on the next `mngr` invocation. There is no measurable startup cost when the venv is already up to date.
