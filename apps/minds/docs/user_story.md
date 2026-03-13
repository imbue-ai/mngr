This is the primary flow for how a user would create a mind for the first time:

1. User starts the forwarding server: `mind forward`
2. User visits `http://localhost:8420` (or `http://localhost:8420/?git_url=https://github.com/user/repo`)
3. Since no agents exist, the landing page shows a creation form with a git URL field (pre-filled if provided via query param)
4. User enters the git URL and clicks Create
5. The forwarding server clones the repository, resolves the agent type from `minds.toml` (or uses `claude-mind`), generates an agent ID, moves the repo to `~/.minds/<agent-id>/`, and runs `mng create --type <type> --id <id> --in-place --label mind=true`
6. While creating, the user sees a progress page that polls for status
7. When creation completes, the user is automatically authenticated and redirected to their mind's web interface at `/agents/<agent-id>/web/`

For subsequent visits:
- If the user has exactly one authenticated agent, they are automatically redirected to it
- If they have multiple agents, they see a listing page with links to each

Creating additional agents:
- Users can visit `/create` to create another mind
- Programmatic creation is available via `POST /api/create-agent` with `{"git_url": "..."}`, polling `GET /api/create-agent/{agent_id}/status` for progress

The point of this whole flow is to make it as easy as possible for users to get a mind running.
