# changelings

Run your own persistent, specialized AI agents

## Overview

changelings is an application that makes it easy to create and deploy persistent, specialized AI agents that are *fully* yours.

Each changeling is a specific sub-type of `mng` agent. While `mng` agents can be any process running in a tmux session, changelings additionally *must*:

1. Serve a web interface (so that it is easy for users to interact with them)
2. Be conversational (able to receive messages from the user and generate responses)

Other than that, the design of each changeling is completely open -- you can customize the agent's behavior, the data it has access to, and the way it responds to messages in any way you want.

## Terminology

- **changeling**: a persistent `mng` agent that serves a web interface and is conversational. Each changeling is identified by its `AgentId` and is labeled with `changeling=true` for discovery via `mng list`. For local deployments, the changeling has a repo directory at `~/.changelings/<agent-id>/` containing a `.mng/settings.toml` with an "entrypoint" create template, and the agent runs directly in this directory via `mng create --in-place`. For remote deployments (Modal, Docker), a temporary repo is prepared and the code is copied to the remote host via `mng create --in <provider>`.
- **forwarding server**: a local process (started via `changeling forward`) that handles authentication and proxies web traffic from the user's browser to the appropriate changeling's web server. Users access all their changelings through such gateways.

## Architecture

The forwarding servers provide:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible changelings
- Reverse proxying of HTTP and WebSocket traffic to individual changeling web servers using Service Worker-based path rewriting

Each changeling may run one or more web servers on separate ports. The forwarding server multiplexes access to all of them under path prefixes (e.g. `/agents/{agent_id}/{server_name}/`). Navigating to `/agents/{agent_id}/` shows a listing of all available servers for that agent.

## Design

See [./docs/design.md](./docs/design.md) for more details on the design principles and architecture of changelings.
