Removed the hand-rolled `OpenCodeAgentConfig.merge_with` override (surfaced by the `identify-suspicious-edge-cases` skill). It reimplemented config merging incorrectly and is now inherited from the base `AgentTypeConfig`, matching the analogous `codex` agent type. This fixes several latent bugs in how `opencode`-derived custom agent types merge:

- Config fields the override never copied (`plugin`, `extra_provision_command`, `upload_file`, `create_directory`, `env`, `env_file`) were silently dropped on every merge; they are now preserved.
- `cli_args` now follows the framework-wide assign-by-default merge contract (use the `cli_args__extend` operator for additive behavior) instead of always concatenating, and an explicitly emptied `cli_args` is no longer ignored.
- A secondary config file that redefines the same custom type without repeating `parent_type` (parsed as the base `AgentTypeConfig`) no longer raises a spurious `ConfigParseError`.
