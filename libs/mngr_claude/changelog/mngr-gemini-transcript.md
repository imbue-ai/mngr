`ClaudeAgent` now satisfies the new `HasTranscriptMixin` and
`HasCommonTranscriptMixin` mixins on `AgentInterface` (introduced to give every
agent type a shared transcript-capture contract). The user-visible behavior of
`mngr transcript <claude-agent>` is unchanged.
