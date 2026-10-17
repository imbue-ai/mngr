Dropped the section comments in `conftest_hooks_test.py` that labelled which block of tests
covered which helper (`# _read_lock_info / _write_lock_info` and eight others). Every test in
the file already names its subject -- `test_read_lock_info_invalid_json`,
`test_break_stale_lock_expired_deadline_kills` -- so the labels restated the names directly
below them and were one rename away from being wrong.

Test-only, and no test was added, removed, or renamed: the same 36 tests run.
