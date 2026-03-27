# Glossary

There are several key concepts to understand when working with minds:

- **mind**: a collection of persistent mngr agents (called **role agents**) that serve a web interface and support chat input/output. The role agents coordinate through shared event streams in a common git repo. Each mind is identified by its `AgentId` and is labeled with `mind=true` for discovery via `mngr list`. The mind has a repo directory at `~/.minds/<agent-id>/`. The agent type can be specified in `minds.toml` or defaults to `claude-mind`.
- **role agent**: a standard mngr agent that fulfills a specific role within a mind (e.g., thinking, working, verifying). Each role agent is created via `mngr create`, appears in `mngr list`, and has its own lifecycle. Multiple instances of the same role can run simultaneously (e.g., several workers).
- **supporting service**: a background process running alongside a role agent (e.g., watchers, web server). These are *not* mngr agents -- they don't appear in `mngr list` and have no lifecycle state. They are infrastructure provisioned automatically by the mind plugin.
- **forwarding server**: a local process (started via `mind forward`) that handles authentication and proxies web traffic from the user's browser to the appropriate mind's web server. Since a user may have *multiple minds* running simultaneously, the forwarding server multiplexes access to all of them through a single local endpoint, handling discovery, routing, and authentication centrally. The forwarding server can also create new minds from git repositories.
