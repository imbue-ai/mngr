This is the primary flow for how a user would create a mind for the first time:

1. User starts the forwarding server: `mind`
2. The server prints a one-time login URL to the terminal
3. User visits the login URL to authenticate (sets a global session cookie)
4. Since no agents exist, the landing page shows a creation form with a git URL field (can also be pre-filled via `/?git_url=...`)
5. User enters the git URL and clicks Create
6. The forwarding server clones the repository, loads settings from `minds.toml`, adds configured vendor repos as git subtrees, resolves the agent type (or uses `claude-mind`), generates an agent ID, and runs `mng create --type <type> --id <id> --in-place --label mind=true`
7. While creating, the user sees a progress page that polls for status
8. When creation completes, the user is redirected to their mind's web interface at `/agents/<agent-id>/web/`

For subsequent visits:
- If the user has exactly one known agent, they are automatically redirected to it
- If they have multiple agents, they see a listing page with links to each

Creating additional agents:
- Users can visit `/create` to create another mind
- Programmatic creation is available via `POST /api/create-agent` with `{"git_url": "..."}`, polling `GET /api/create-agent/{agent_id}/status` for progress

The point of this whole flow is to make it as easy as possible for users to get a mind running.
