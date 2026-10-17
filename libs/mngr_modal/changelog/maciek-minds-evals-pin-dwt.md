Marked `test_upload_deploy_files_handles_large_set_on_modal` `@pytest.mark.flaky`, so offload retries
it instead of failing the run.

The fresh Modal sandbox it creates can accept TCP before sshd answers the handshake, so the
connection fails ("No existing session") before the bulk upload the test actually guards ever
starts -- the same fresh-sandbox sshd boot race its already-marked neighbours were marked for.
Observed on a CI run whose changes were confined to `apps/minds_evals`, passing on the retry.

The marker only buys a retry; the underlying race is unchanged and still worth fixing at the source.
