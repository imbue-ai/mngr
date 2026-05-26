# CI guard for stale generated CLI docs

`scripts/make_cli_docs.py` gained a `--check` mode that reports any stale
generated docs (and the exact regen command) and exits non-zero without writing
anything. Its content generation was refactored so a single
`collect_generated_files()` function is the shared source of truth for both
writing the docs and checking them, so the writer and checker cannot drift.

A new `test_cli_docs_are_up_to_date` (in `test_meta_ratchets.py`, alongside the
existing repo-wide ruff check) runs that `--check` mode and fails if the
committed CLI docs or PyPI README are out of date, pointing you at
`uv run python scripts/make_cli_docs.py`. This complements the existing
`test_all_non_hidden_commands_have_generated_docs`, which only checks that a doc
file exists per command, by also verifying the file contents are current.
