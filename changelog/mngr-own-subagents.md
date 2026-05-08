- New experimental plugin `mngr_claude_subagent_proxy` reroutes Claude
  Code's built-in `Task` (Agent) tool through mngr-managed subagents
  via a Haiku dispatcher. Users can `mngr connect` to the spawned
  subagent and observe its progress; the parent still receives a
  normally-shaped `tool_result`. The wait-script invokes
  `mngr create --type mngr-proxy-child`, tags the child with
  `mngr_claude_subagent_proxy_parent_{name,id}` + `_tool_use_id`
  labels for parent↔child queries via `mngr list --format json`,
  and tails the child's transcript JSONL until a terminal stop
  reason. Project / plugin Stop hooks are auto-guarded with an
  env-conditional `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD` prefix so they
  no-op inside spawned subagents (otherwise an autofix orchestrator
  in the parent will hold its child responsible for the parent's
  uncommitted changes / failing CI). See `libs/mngr_claude_subagent_proxy/README.md`
  for the full architecture, label schema, deferred work, and
  experimental-status banner.
