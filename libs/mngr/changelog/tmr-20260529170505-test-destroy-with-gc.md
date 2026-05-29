Fixed the `test_destroy_with_gc` tutorial e2e test (`mngr destroy my-task --force --gc`):

- Added a `@pytest.mark.timeout(90)` override so the destroy-plus-garbage-collection
  command has time to complete (the global 10s pytest timeout was firing before the
  command finished).
- Removed the superfluous `@pytest.mark.modal` mark. This localhost `--type command`
  test never provisions a Modal environment, so gc only performs an in-subprocess
  Modal lookup that the resource guard cannot detect; gc skips the unavailable Modal
  provider gracefully, so the test neither requires nor detectably invokes Modal.
- Strengthened assertions to verify the actual effects: the agent is reported
  destroyed, garbage collection runs, and the agent no longer appears in `mngr list`.
