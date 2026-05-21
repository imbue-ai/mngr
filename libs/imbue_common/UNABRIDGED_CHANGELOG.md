# Unabridged Changelog - imbue_common

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/imbue_common/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-08

- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
