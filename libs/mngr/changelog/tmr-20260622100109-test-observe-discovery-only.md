Fixed and strengthened the `mngr observe --discovery-only` release test (`test_observe_discovery_only`).

- Removed the spurious `@pytest.mark.modal` mark: in a fresh, empty environment observe's discovery skips the Modal provider (the environment does not exist yet) and never invokes the `modal` CLI, the only Modal usage the resource guard can observe across the mngr subprocess boundary, so the mark tripped the guard's "marked but never invoked" check.

- The test previously masked the command's exit code with `|| true` and only ran observe for one second, so it asserted essentially nothing. It now runs observe long enough to emit discovery events, asserts the wrapper exits 124 (proving observe streamed continuously until the timeout killed it rather than crashing), and verifies that every emitted stdout line is a well-formed `mngr/discovery` JSONL event.
