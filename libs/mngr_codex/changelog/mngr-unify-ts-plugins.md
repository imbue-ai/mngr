Added a conformance test asserting that codex's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`). The release test now runs on the
shared agent release-lifecycle harness (`imbue.mngr.utils.agent_release_testing`).

(Surfaced while running the harness: codex's post-restart message send currently times
out against the real binary -- the resumed TUI does not echo the tmux paste within the
send timeout. This reproduces on the pre-unification test too, so it is a pre-existing
codex resume-send issue, documented in the release test, to be fixed separately.)
