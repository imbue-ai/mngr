Removed source line-number references from the `api/list` and `api/gc` test suites.

Six section banners in `list_test.py` labelled groups of tests with line ranges in `list.py` (for example `# Lines 396-405: Error differentiation in _handle_listing_error`). Those ranges had drifted by 91 to 344 lines as `list.py` grew, so following any of them landed in an unrelated function. The banners are removed outright; each test's name already states the function and branch it covers.

Three docstrings additionally pointed at a specific line or internal helper call. They now describe the behavior being verified instead, so they no longer depend on where the implementation happens to live. One of them also claimed the discovery events file must contain the legacy whole-fleet snapshot, which the test has asserted is absent since per-provider snapshots superseded it; the docstring now matches what the test checks.
