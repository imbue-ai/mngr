Hardened suspicious edge-case handling across `imbue/mngr/cli` (and a small change
in `api/find.py`). These changes make defensively-written code fail loudly instead
of silently producing wrong output, narrow over-broad exception handling, and
remove dead fallbacks. User-visible highlights:

- `mngr destroy` now supports `--on-error abort|continue` (default `abort`).
  Previously a batch destroy with `--force` (e.g. `mngr list --ids | mngr destroy
  - --force`) discarded the entire batch and destroyed nothing if even one id was
  stale. `--force` is now confirmation-skip only: with `--on-error continue`,
  destroy removes the agents that exist and warns about the missing identifiers;
  the default `--on-error abort` aborts when any named agent is not found.
- `mngr extras` no longer guesses a shell for an unrecognized `$SHELL`; it prompts
  interactively or skips with a clear "only bash/zsh supported" message instead of
  writing a completion script to an rc file your shell may never source.
- `mngr plugin add` now fails clearly when a path source has a missing/invalid
  `pyproject.toml` (matching `plugin remove`), and reports an unresolved git
  package name instead of substituting the git URL.
- `mngr plugin list --fields` now rejects unknown field names instead of rendering
  a blank column; `mngr complete --script <shell>` rejects unsupported shells.
- Snapshot discovery, config defaults, transcript support checks, and various
  other paths now surface real auth/config/schema/type errors instead of masking
  them as "not found", "success", or empty output.
- `mngr start` now errors if a just-started agent is unexpectedly absent from its
  host rather than silently reporting success; `mngr create --reuse` warns (still
  creates) when a matched agent vanishes after host start.

Internal: replaced built-in `AssertionError` "impossible case" guards with
`SwitchError`, narrowed several over-broad `try`/`except` blocks, removed dead
code, and added/updated tests for the new fail-loud behavior.
