Removed a monorepo-development-only paragraph (the `~/.local/bin` pre-commit shim note) from the top-level README so the published PyPI README stays focused on user-relevant content.

`make_cli_docs.py` now also generates the provider/agent config tables in each plugin README from the Pydantic field descriptions (the source of truth, also shown by `mngr config`), spliced between markers and verified by the docs `--check` gate so the tables can no longer drift from the code.

The `regenerate-cli-docs` pre-commit hook now runs `make_cli_docs.py --check` (non-mutating, covering every generated file) instead of regenerating in place and diffing only the mngr command docs, and its trigger now includes the provider/agent `config.py` / `plugin.py` sources and generated provider READMEs. Previously, drift in the generated provider README tables could slip past the hook.

The provider/agent config tables are rendered entirely from the model: each table only names its config class and which inherited base fields to also surface, and the field names, defaults, and descriptions are derived automatically (a small per-field override covers non-literal defaults like "gcloud/ADC default"). A field added to a config model now appears in its table automatically, so it can't silently vanish.
