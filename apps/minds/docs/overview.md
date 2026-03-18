# How it works

Each mind is a collection of persistent `mng` agents that, together, form a single higher-level "agent" from the perspective of the end-user. A mind *must*:

1. Serve a web interface (so that it is easy for users to interact with them)
2. Support chat input/output (able to receive messages from the user and generate responses)

Other than that, the design of each mind is completely open -- you can customize the agent's behavior, the data it has access to, and the way it responds to messages in any way you want.

## Architecture

The forwarding server provides:
- Authentication via one-time codes and signed cookies
- A landing page listing all accessible minds (or a creation form if none exist)
- Agent creation from git repositories via a web form or API
- Reverse proxying of HTTP and WebSocket traffic to individual mind web servers using Service Worker-based path rewriting

Each mind may run one or more web servers on separate ports. The forwarding server multiplexes access to all of them under path prefixes (e.g. `/agents/{agent_id}/{server_name}/`). Navigating to `/agents/{agent_id}/` shows a listing of all available servers for that agent.

## Creating agents

Agents can be created in two ways:

1. **Via the web UI**: Visit the forwarding server. If no agents exist, you'll see a creation form. Enter a git repository URL and submit. You can also pre-fill the URL via query parameter: `http://localhost:8420/?git_url=https://github.com/user/repo`

2. **Via the API**: POST to `/api/create-agent` with a JSON body containing `git_url`. Poll `/api/create-agent/{agent_id}/status` for creation progress.
