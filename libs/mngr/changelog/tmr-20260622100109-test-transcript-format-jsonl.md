Fixed the `test_transcript_format_jsonl` release test, which tripped the resource guard because it carried a superfluous `@pytest.mark.rsync` mark even though creating a local claude agent and reading its transcript never invokes rsync. Dropped the stale mark.

Strengthened the same test to verify the JSONL output faithfully carries the conversation (the user message that was sent and at least one assistant reply) instead of only checking that each line parses as a JSON object.
