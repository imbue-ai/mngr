Fixed the `test_create_modal_rsync` e2e release test so it keeps the Modal host
alive for verification. The test now pins a lightweight `--type command -- sleep`
agent (matching the rest of the suite) instead of relying on the default agent,
which died without API credentials and let the host auto-stop before the
follow-up `exec` could inspect the rsync-transferred files.
