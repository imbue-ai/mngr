# changelings

Run your own persistent, specialized AI agents

## Overview

changelings is an application that makes it easy to create and deploy persistent, specialized AI agents that are *fully* yours.

Each changeling is a collection of persistent `mng` agents that, together, form a single higher-level "agent" from the perspective of the end-user. A changeling *must*:

1. Serve a web interface (so that it is easy for users to interact with them)
2. Support chat input/output (able to receive messages from the user and generate responses)

Other than that, the design of each changeling is completely open -- you can customize the agent's behavior, the data it has access to, and the way it responds to messages in any way you want.

## Terminology

- **changeling**: a collection of persistent mng agents (called **role agents**) that serve a web interface and support chat input/output. The role agents coordinate through shared event streams in a common git repo. Each changeling is identified by its `AgentId` and is labeled with `changeling=true` for discovery via `mng list`. For local deployments, the changeling has a repo directory at `~/.changelings/<agent-id>/`. The agent type is specified via `--agent-type` on the CLI or the `agent_type` field in `changelings.toml`. For remote deployments (Modal, Docker), a temporary repo is prepared and the code is copied to the remote host via `mng create --in <provider>`.
- **role agent**: a standard mng agent that fulfills a specific role within a changeling (e.g., thinking, working, verifying). Each role agent is created via `mng create`, appears in `mng list`, and has its own lifecycle. Multiple instances of the same role can run simultaneously (e.g., several workers).
- **supporting service**: a background process running alongside a role agent (e.g., watchers, web server). These are *not* mng agents -- they don't appear in `mng list` and have no lifecycle state. They are infrastructure provisioned automatically by the changeling plugin.
- **forwarding server**: a local process (started via `changeling forward`) that handles authentication and proxies web traffic from the user's browser to the appropriate changeling's web server. Since a user may have *multiple changelings* running simultaneously, the forwarding server multiplexes access to all of them through a single local endpoint, handling discovery, routing, and authentication centrally.

## Architecture

The forwarding servers provide:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible changelings
- Reverse proxying of HTTP and WebSocket traffic to individual changeling web servers using Service Worker-based path rewriting

Each changeling may run one or more web servers on separate ports. The forwarding server multiplexes access to all of them under path prefixes (e.g. `/agents/{agent_id}/{server_name}/`). Navigating to `/agents/{agent_id}/` shows a listing of all available servers for that agent.

## Design

See [./docs/design.md](./docs/design.md) for more details on the design principles and architecture of changelings.
