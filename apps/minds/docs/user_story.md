This is the primary flow for how a user would create a mind for the first time:

1. User starts the forwarding server: `mind`
2. The server prints a one-time login URL to the terminal
3. User visits the login URL to authenticate (sets a global session cookie)
4. Since no agents exist, the landing page shows a creation form with fields for agent name, git repository URL (or local path), branch, and launch mode (DEV/LOCAL/CLOUD)
5. User fills in the form and clicks Create
6. The forwarding server clones the repository to a temp directory (if a URL) or uses the local path directly, generates an agent ID, and runs `mngr create <name> --id <id> --no-connect --label mind=<name> --template main --template <mode>`. If Cloudflare credentials are configured, it also creates a tunnel and injects the tunnel token into the agent.
7. While creating, the user sees a progress page that polls for status
8. When creation completes, the user is redirected to their mind's web interface at `/agents/<agent-id>/web/`

For subsequent visits:
- If the user has exactly one known agent, they are automatically redirected to it
- If they have multiple agents, they see a listing page with links to each

Creating additional agents:
- Users can visit `/create` to create another mind
- Programmatic creation is available via `POST /api/create-agent` with `{"git_url": "..."}`, polling `GET /api/create-agent/{agent_id}/status` for progress

The point of this whole flow is to make it as easy as possible for users to get a mind running.
