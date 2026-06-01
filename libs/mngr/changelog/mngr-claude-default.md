# Make `CreateAgentOptions.agent_type` required

- `CreateAgentOptions.agent_type` is now a required field (previously
  `AgentTypeName | None` defaulting to `None`). Following the removal of
  the CLI's implicit `claude` default, the residual `agent_type or
  AgentTypeName("claude")` fallbacks in `api.create.create` and
  `Host.create_agent_state` were the last places that silently defaulted
  an unset type to `claude`. Both fallbacks are gone, and the type system
  now guarantees every agent-creation path supplies a concrete type. The
  now-dead `if options.agent_type is not None:` guard around agent-type
  provisioning merging in `Host.provision_agent` was also dropped.
