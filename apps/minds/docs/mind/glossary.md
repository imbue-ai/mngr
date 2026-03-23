# Glossary

There are several key concepts to understand when working with minds:

- **mind**: a collection of persistent mng agents (called **role agents**) that serve a web interface and support chat input/output. The role agents coordinate through shared event streams in a common git repo. Each mind is identified by its `AgentId` and is labeled with `mind=true` for discovery via `mng list`. The mind has a repo directory at `~/.minds/<agent-id>/`. The agent type can be specified in `minds.toml` or defaults to `claude-mind`.
- **role agent**: a standard mng agent that fulfills a specific role within a mind (e.g., thinking, working, verifying). Each role agent is created via `mng create`, appears in `mng list`, and has its own lifecycle. Multiple instances of the same role can run simultaneously (e.g., several workers).
- **supporting service**: a background process running alongside a role agent (e.g., watchers, web server). These are *not* mng agents -- they don't appear in `mng list` and have no lifecycle state. They are infrastructure provisioned automatically by the mind plugin.
- **forwarding server**: a local process (started via `mind forward`) that handles authentication and proxies web traffic from the user's browser to the appropriate mind's web server. Since a user may have *multiple minds* running simultaneously, the forwarding server multiplexes access to all of them through a single local endpoint, handling discovery, routing, and authentication centrally. The forwarding server can also create new minds from git repositories.
