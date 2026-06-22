Hardened the end-to-end tutorial test fixture so discovery commands (e.g. `mngr list`, and the garbage-collection pass run by `mngr destroy`) no longer fail in environments that lack cloud credentials or a Docker daemon.

The `e2e` test fixture now disables provider backends that the environment cannot reach: the cloud backends e2e never exercises (aws, azure, gcp, imbue_cloud, vultr) are always disabled, while Docker and Modal are disabled only when no daemon / credentials are detected. Previously an unconfigured AWS or Azure provider made `mngr list` exit non-zero (Azure even blocked for ~30s), and an absent Docker daemon or Modal credentials broke list and the post-destroy gc.

Also fixed `test_destroy_all_via_stdin`: removed a superfluous `@pytest.mark.rsync` mark (local-provider agents never invoke rsync) and added an assertion on the "Successfully destroyed N agent(s)" summary count to confirm the full piped id list reaches `mngr destroy`.
