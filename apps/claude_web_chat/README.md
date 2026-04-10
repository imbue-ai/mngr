# Claude Web Chat

Web chat interface for viewing and interacting with mngr-managed Claude agents.

Shows live conversations from Claude session files in a web UI, with real-time
updates via Server-Sent Events.

## Usage

```bash
claude-web-chat
```

Opens at http://127.0.0.1:8000 by default.

## Development

```bash
# Backend
cd apps/claude_web_chat
uv run claude-web-chat

# Frontend (with hot reload)
cd apps/claude_web_chat/frontend
npm install
npm run dev
```

## Building

```bash
cd apps/claude_web_chat/frontend
npm run build
```

This compiles the frontend into `imbue/claude_web_chat/static/`.
