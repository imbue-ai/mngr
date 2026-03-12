This is the primary flow for how a user would deploy a mind for the first time:

1. User runs: `mind deploy --type elena-code` (or `mind deploy <git-url>` for an existing repo with agent_type in minds.toml)
2. (User gets through various auth flows, now has tokens -- just assume this exists for now, we'll set the right env vars)
3. User answers some questions:
   - What do you want to name the agent? [<agent-type> | <type something>]
   - Where do you want this agent to run? [local | modal | docker]
   - Do you want this agent to be able to launch its own agents? [yes | not now]
   - [future] Do you want to access this agent from anywhere besides this computer? [yes (requires forwarding server) | not now]
   - [future] Do you want to receive mobile notifications from this agent? [yes (requires notification setup) | not now]
4. We prepare a temporary repo (either by cloning or by creating an empty git repo), resolve the agent type (from --type or minds.toml), generate an agent ID, and move the repo to `~/.minds/<agent-id>/`. For local deployment, we run `mng create --in-place --id <id> --type <type> --label mind=true` from that directory. For remote deployment, we run `mng create --in <provider> --id <id> --type <type> --source-path <dir> --label mind=true` and clean up the local directory afterwards.
   If the user wants the agent to be able to run its own agents and tasks, we ensure that `mng` is injected as well.
5. We ensure a local forwarding server daemon process is running (for forwarding web requests and handling authentication). One-time auth codes are generated and stored for the new mind.
6. We're done: print the associated URL where the agent can be accessed (e.g. http://localhost:8420/agents/<agent-id>/)

The point of this whole flow is to make it as easy as possible for users to deploy a new mind.
