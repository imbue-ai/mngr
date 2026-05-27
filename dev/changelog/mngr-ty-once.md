# Consolidated ty/ruff ratchet tests to run once repo-wide

The per-project `test_no_type_errors` and `test_no_ruff_errors` tests (~36 copies,
one per workspace member) were redundant: `ty check` resolves the uv workspace
root (root `pyproject.toml` declares `[tool.uv.workspace] members = ["libs/*",
"apps/*"]`) and scans every member on each invocation regardless of the directory
it runs from, and the repo-wide ruff check is a strict superset of the per-project
ruff checks. Each duplicate invocation was a full ~0.8s cold workspace scan with
no cross-process cache benefit.

Removed the per-project copies and kept a single repo-wide `test_no_type_errors`
and `test_no_ruff_errors` in `test_meta_ratchets.py`, updating the meta-ratchet
expected-test-name set and `CLAUDE.md` accordingly.

Because `ty` (unlike `ruff`) was not in pre-commit, scoped local runs such as
`just test-quick libs/<project>` no longer type-checked at all after the
consolidation. Added a `ty` pre-commit hook (mirroring the existing `ruff` hook,
running `uv run ty check` over the whole workspace whenever a Python file is
staged) so local commits still get a type-check gate; the single
`test_no_type_errors` in `test_meta_ratchets.py` remains the CI backstop.

No user-facing behavior change.
