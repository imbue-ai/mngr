# Fixed opencode agent-type config merging

Removed the custom `OpenCodeAgentConfig.merge_with` override, which had two bugs:

- It concatenated `cli_args` instead of replacing them, diverging from the
  documented assign-by-default merge semantics used by every other agent type
  (use the `field__extend` operator for additive behavior).
- It silently dropped every config field except `parent_type`, `cli_args`, and
  `command` when merging, so settings like `env` and `extra_provision_command`
  on an opencode agent type were lost whenever the config was merged with an
  override.

The opencode config now inherits `AgentTypeConfig.merge_with`, which handles its
single added field (`command`) correctly and preserves all other fields.
