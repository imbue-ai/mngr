# minds

Run your own persistent, specialized AI agents

## Overview

minds is an application that makes it easy to create and run persistent, specialized AI agents that are *fully* yours.

Each mind is a collection of persistent `mng` agents that, together, form a single higher-level "agent" from the perspective of the end-user. A mind *must*:

1. Serve a web interface (so that it is easy for users to interact with them)
2. Support chat input/output (able to receive messages from the user and generate responses)

Other than that, the design of each mind is completely open -- you can customize the agent's behavior, the data it has access to, and the way it responds to messages in any way you want.

## Terminology

- **mind**: a collection of persistent mng agents (called **role agents**) that serve a web interface and support chat input/output. The role agents coordinate through shared event streams in a common git repo. Each mind is identified by its `AgentId` and is labeled with `mind=true` for discovery via `mng list`. The mind has a repo directory at `~/.minds/<agent-id>/`. The agent type can be specified in `minds.toml` or defaults to `claude-mind`.
- **role agent**: a standard mng agent that fulfills a specific role within a mind (e.g., thinking, working, verifying). Each role agent is created via `mng create`, appears in `mng list`, and has its own lifecycle. Multiple instances of the same role can run simultaneously (e.g., several workers).
- **supporting service**: a background process running alongside a role agent (e.g., watchers, web server). These are *not* mng agents -- they don't appear in `mng list` and have no lifecycle state. They are infrastructure provisioned automatically by the mind plugin.
- **forwarding server**: a local process (started via `mind forward`) that handles authentication and proxies web traffic from the user's browser to the appropriate mind's web server. Since a user may have *multiple minds* running simultaneously, the forwarding server multiplexes access to all of them through a single local endpoint, handling discovery, routing, and authentication centrally. The forwarding server can also create new minds from git repositories.

## Architecture

The forwarding server provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible minds (or a creation form if none exist)
- Agent creation from git repositories via a web form or API
- Reverse proxying of HTTP and WebSocket traffic to individual mind web servers using Service Worker-based path rewriting

Each mind may run one or more web servers on separate ports. The forwarding server multiplexes access to all of them under path prefixes (e.g. `/agents/{agent_id}/{server_name}/`). Navigating to `/agents/{agent_id}/` shows a listing of all available servers for that agent.

## Getting started

```bash
# Start the forwarding server
mind forward

# Visit http://localhost:8420 in your browser
# If no agents exist, you'll see a form to create one from a git URL
# The agent will be created and you'll be redirected to it automatically
```

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the forwarding server. If no agents exist, you'll see a creation form. Enter a git repository URL and submit. You can also pre-fill the URL via query parameter: `http://localhost:8420/?git_url=https://github.com/user/repo`

2. **Via the API**: POST to `/api/create-agent` with a JSON body containing `git_url`. Poll `/api/create-agent/{agent_id}/status` for creation progress.

## Design

See [./docs/design.md](./docs/design.md) for more details on the design principles and architecture of minds.
