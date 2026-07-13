Regenerated the `mngr imbue_cloud` CLI reference docs for the new `sync` subcommand group (workspace-record and key-bundle transport used by the minds app).

Marked `test_worker_hubs_do_not_accumulate_across_polls` as flaky: its live-gevent-hub count races against concurrently-running tests under xdist (it passes reliably in isolation).
