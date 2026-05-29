# Simplify `--adopt-session` agent-type validation

- Now that `CreateAgentOptions.agent_type` is always set (it became a
  required field), the `--adopt-session` `on_before_create` validation no
  longer special-cases an unset type: it simply requires the agent type
  to be `claude`. No behavior change for users, since the CLI already
  requires a concrete agent type.
