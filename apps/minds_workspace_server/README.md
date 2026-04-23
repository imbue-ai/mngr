# Minds Workspace Server

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time updates via Server-Sent Events.

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

## Building

```bash
cd apps/minds_workspace_server/frontend
npm run build
```

This compiles the frontend into `imbue/minds_workspace_server/static/`.
