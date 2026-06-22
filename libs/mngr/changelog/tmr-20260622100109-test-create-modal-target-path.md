Fixed the `test_create_modal_target_path` tutorial e2e test (covering `mngr create my-task@.modal:/workspace`).

The verification step used a bare `mngr list --format json`, which enumerates every enabled provider backend and -- under the default `--on-error abort` -- fails loudly when any one is unavailable (e.g. the AWS backend with no credentials in the isolated e2e environment). Scoped the listing to `mngr list --provider modal --format json` so discovery stays targeted at the provider that actually owns the agent under test, matching the rest of the Modal create tests.

Also strengthened the verification: the test now confirms the transferred git repository is actually mounted at the target path (`/workspace/.git` present) in addition to checking the recorded `work_dir` and the agent's runtime `pwd`, proving the work directory -- not merely the shell cwd string -- landed at the requested mount point.
