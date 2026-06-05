Fixed the `test_observe_discovery_recap` e2e release test, which covers the
`mngr observe --discovery-only` tutorial block. The test was marked
`@pytest.mark.modal`, but on a fresh test environment (no Modal agents created)
the discovery stream deliberately skips the Modal provider because its
environment does not exist yet, so no Modal call is ever made. The resource
guard therefore failed the test for declaring a Modal dependency it never
exercised. Removed the superfluous mark and strengthened the test to assert that
the stream actually emits a `DISCOVERY_FULL` JSONL snapshot, rather than only
checking that the command exits cleanly.
