Fixed the CLEANING UP RESOURCES tutorial e2e tests (`test_gc.py`). Each test now
creates a real Modal-backed `command` agent before running `mngr gc`, so the
commands genuinely exercise Modal (satisfying `@pytest.mark.modal`/`rsync`) and
have a real provider environment to scan. Also added per-test timeouts so the
slow Modal queries no longer trip the default 10s pytest timeout. The
`mngr gc --dry-run` test additionally asserts the dry-run preview is reported
and that it leaves the agent untouched.
