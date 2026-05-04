# Minds Workspace Server

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time
updates via Server-Sent Events.

## Usage

```bash
minds-workspace-server
```

Opens at http://127.0.0.1:8000 by default.

## Development

```bash
# Backend
cd apps/minds_workspace_server
uv run minds-workspace-server

# Frontend (with hot reload)
cd apps/minds_workspace_server/frontend
npm install
npm run dev
```

## Refreshing web-service tabs from an agent

An agent running inside the workspace container can tell the user's Minds UI
to reload any open tab for one of its web services. The agent POSTs to the
workspace server on localhost (default port 8000, matching
`Config.minds_workspace_server_port`):

```bash
curl -X POST "http://127.0.0.1:8000/api/refresh-service/web"
```

This appends a `refresh_service` event to the agent's
`events/refresh/events.jsonl` file. The minds desktop client tails the event
via `mngr event --follow`, then POSTs back to the workspace server which
broadcasts a WebSocket message telling the frontend to reload every open
iframe tab tied to the given service (matched by the iframe's
`data-service-name` attribute). Replace `web` with whichever service name
(as listed in `runtime/applications.toml` / the tab dropdown) you want to
refresh.

## Building

```bash
cd apps/minds_workspace_server/frontend
npm run build
```

This compiles the frontend into `imbue/minds_workspace_server/static/`.
