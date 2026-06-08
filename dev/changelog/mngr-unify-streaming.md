Updated `uv.lock` to add the `anthropic` package (and its transitive `docstring-parser`
dependency), newly required by `libs/mngr_claude` for the shared typed Claude stream-json envelope.
The substantive change lives under `libs/mngr_claude` (see that project's changelog); this is the
root-level lockfile update that pins the resolved dependency tree.
