# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

Also removed the now-unused `check_no_ruff_errors` helper from
`imbue/imbue_common/ratchet_testing/ratchets.py`: its only callers were the
deleted per-project `test_no_ruff_errors` tests, and the repo-wide ruff test
runs its own `ruff check` / `ruff format --check` invocations rather than using
the helper. (`check_no_type_errors` is kept, since the repo-wide type test uses it.)

No user-facing behavior change.
