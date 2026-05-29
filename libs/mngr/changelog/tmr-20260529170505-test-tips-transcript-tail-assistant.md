Fixed the `test_tips_transcript_tail_assistant` e2e tutorial test. `mngr transcript`
only renders transcripts for agent types that emit one (e.g. claude), so the test
now sets up an agent that has produced a synthetic common-transcript and asserts on
the actual rendered output: `--role assistant` filters out user/tool messages and
`--tail 5` keeps only the last five assistant messages. Added a companion
unhappy-path test asserting that `mngr transcript` fails with a clear error for an
agent type that does not produce a transcript.
