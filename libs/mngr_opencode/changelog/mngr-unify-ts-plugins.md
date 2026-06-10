Added a node-harness conformance test asserting that opencode's real emitted
common-transcript records validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`) -- also the first CI-runnable check of
opencode's in-process TypeScript emitter (previously covered only by the non-CI release
test). The release test now runs on the shared agent release-lifecycle harness
(`imbue.mngr.agents.agent_release_testing`).
