# `HasTranscriptMixin` formalises the raw-capture contract for `mngr transcript`

A new `HasTranscriptMixin` on `AgentInterface` formalises the raw-capture
contract; `HasCommonTranscriptMixin` extends it with the (gated) common
converter on top. Future agent types get `mngr transcript` support for free
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts`
and shipping the matching per-agent scripts.
