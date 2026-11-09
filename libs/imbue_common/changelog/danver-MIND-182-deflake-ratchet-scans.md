# Ratchet tests load their file scan during setup, outside their timeout

Ratchet checks share their work through process-wide caches: the file listing, every file's
contents, and every Python file's parsed AST. Those caches used to fill inside the body of whichever
ratchet test ran first in a process, so that one test paid for reading and parsing the whole tree
inside its 10s pytest-timeout. Offload runs every CI batch as its own pytest process, so every batch
had such a test, and under load some of them timed out (MIND-182).

- New pytest marker `ratchet_scan(source_dir)`. The shared conftest hooks preload a marked test's
  scan during pytest setup, which `timeout_func_only = true` leaves untimed, so the test's timeout
  covers only its own check.

- New `preload_text_scan` and `preload_python_ast_scan` in `ratchet_testing.core`, and
  `preload_ratchet_scan_for_test` in `ratchet_testing.standard_ratchet_checks`. Python files are
  parsed only for the tests of the eight checks that read ASTs.

- This project's `test_ratchets.py` declares `pytest.mark.ratchet_scan(_DIR)` in place of the
  `xdist_group` marker, which no test runner here honours.
