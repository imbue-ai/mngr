Test-only flake mitigations (no production code change):

- Made the Docker `test_pull_image_not_found_raises` integration test resilient to a Docker Hub registry-connectivity flake: when the registry is unreachable (the pull times out before returning its 404), the test now skips instead of failing, while still asserting the clean "image not found" path when the registry is reachable. Also marked it `@pytest.mark.flaky` so offload retries it.
- Marked the tmux integration test `test_start_restart_stopped_agent` `@pytest.mark.flaky` (it occasionally exceeds the 10s pytest-timeout by a few hundred ms under CI load), matching its already-flaky siblings (`test_list`, `test_create`, `test_connect`, `test_destroy`) so offload retries it rather than hard-failing.
