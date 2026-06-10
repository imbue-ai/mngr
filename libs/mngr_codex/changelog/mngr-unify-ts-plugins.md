Added a conformance test asserting that codex's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`). The release test now runs on the
shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`). The
full lifecycle (including stop/start resume) passes end-to-end against the real codex
binary. Simplified the release test's plumbing to reuse the shared `init_git_repo` helper
and the autouse fixture's tmux-server isolation instead of hand-rolling its own git repo
setup and private tmux server.
