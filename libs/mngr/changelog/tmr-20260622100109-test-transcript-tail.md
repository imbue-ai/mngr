Fixed the `test_transcript_tail` release test: dropped a superfluous `@pytest.mark.rsync` mark that the resource guard rejected (the test runs entirely against a local claude agent and never invokes rsync, matching its sibling `test_transcript_tail_one`).

Strengthened the same test to verify `mngr transcript --tail N` returns the most recent events in order (the suffix of the full transcript), rather than only checking the event-count cap.
