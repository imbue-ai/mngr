Added a conformance test asserting that pi's real emitted common-transcript records
validate against the new canonical envelope schema
(`imbue.mngr.agents.common_transcript_records`), so the pi emitter and the shared
contract cannot drift. The release test now runs on the shared agent
release-lifecycle harness (`imbue.mngr.utils.agent_release_testing`), holding pi to the
same lifecycle and transcript contract as every other agent.
