Fixed the `test_create_modal_upload_only` e2e tutorial test, which failed with
"No agent type provided" because the isolated test environment configures no
default agent type. The test now pins `--type command -- sleep N` (matching the
rest of the suite) so the `--upload-file` flag is exercised end to end, and it
verifies the uploaded file actually lands on the remote host with the expected
content.
