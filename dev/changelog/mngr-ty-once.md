# Consolidated ty/ruff ratchet tests to run once repo-wide

The per-project `test_no_type_errors` and `test_no_ruff_errors` tests (~36 copies,
one per workspace member) were redundant: `ty check` resolves the uv workspace
root (root `pyproject.toml` declares `[tool.uv.workspace] members = ["libs/*",
"apps/*"]`) and scans every member on each invocation regardless of the directory
it runs from, and the existing repo-wide ruff check in `test_meta_ratchets.py` is
a strict superset of the per-project ruff checks. Each duplicate invocation was a
full ~0.8s cold workspace scan with no cross-process cache benefit.

Added a single `test_no_type_errors_repo_wide` to `test_meta_ratchets.py` (next to
the existing `test_no_ruff_lint_errors_repo_wide`), removed the per-project copies,
and updated the meta-ratchet expected-test-name set and `CLAUDE.md` accordingly.

No user-facing behavior change.
