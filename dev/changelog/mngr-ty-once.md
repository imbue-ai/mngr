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
expected-test-name set accordingly.

Because `ty` (unlike `ruff`) was not in pre-commit, scoped local runs such as
`just test-quick libs/<project>` no longer type-checked at all after the
consolidation. Added a `ty` hook to `.pre-commit-config.yaml` that runs
`uv run ty check` over the whole workspace at the `pre-push` stage (ty can't
scope to staged files, so running it per-commit would add a fixed full-workspace
scan to every commit). Pushes now get a type-check gate; the single
`test_no_type_errors` in `test_meta_ratchets.py` remains the CI backstop.

No user-facing behavior change.
