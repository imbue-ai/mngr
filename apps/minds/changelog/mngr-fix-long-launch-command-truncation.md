Marked `test_backup_restore_rewinds_the_resumed_workspace_in_place` as flaky so CI retries it.

The restore itself succeeds, but restarting the workspace's services at the end intermittently fails with `xvfb: ERROR (spawn error)`, which fails the whole operation and so the test. Observed failing on one CI run and passing on a re-run of the same code. The retry is a stopgap; the underlying service-spawn race still needs fixing.
