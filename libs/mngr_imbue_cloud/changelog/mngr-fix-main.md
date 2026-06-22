Fixed two `imbue_cloud` provider unit tests that broke on `main` after recent merges, so they once again match the production code:

- The `start_host` regression test (and `start_host`'s own docstring) still required an authorized-keys re-seed and a host-key re-scan, but those steps were deliberately removed because a `docker stop`/`docker start` preserves the container filesystem (only the sshd *process*, launched via `docker exec`, needs relaunching). The test and docstring now assert/describe just the sshd relaunch.

- The `get_host` test stub returned a boolean for the `docker inspect` running-state probe, but the probe now reads `{{.State.Status}}` and compares it against the shared `is_running_container_state` rule. The stub now returns a container status string, so a running leased container correctly resolves to an online host.
