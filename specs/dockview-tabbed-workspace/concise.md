# Dockview Tabbed Workspace

## Overview

* Replace the single-purpose chat view in minds_workspace_server with a dockview-based tabbed/split-panel workspace, so each agent gets a flexible IDE-like layout
* Use `dockview-core` (vanilla TypeScript, framework-agnostic) since the frontend is built on Mithril.js, not React
* Default experience is unchanged: selecting an agent opens a single "Chat" tab, preserving familiarity
* Enable full dockview capabilities (tabs, horizontal/vertical splits, drag-and-drop between groups) from the start
* Per-agent layouts persist to disk via backend API, surviving server restarts

## Expected Behavior

* Selecting an agent in the sidebar shows a dockview workspace in the main content area instead of the current header + message list + footer
* The dockview tab bar replaces the current header; the active chat tab's title is the agent name
* The Chat tab contains the existing message list and message input (footer moves inside the panel)
* The Chat tab is unclosable (no close button); other tabs can be closed freely
* A "+" button in the tab bar opens a dropdown menu with three tab types:
  - **Chat** -- focuses the existing chat tab (singleton per agent)
  - **Terminal** -- opens an iframe to `http://localhost:7681` (ttyd default)
  - **Custom URL** -- shows a dialog with a URL field and an optional title field; title defaults to the URL hostname if not provided
* Terminal and Custom URL tabs render their content in iframes
* Users can drag tabs to reorder, split panels horizontally/vertically, and drag tabs between groups
* Subagent conversations (currently navigated via a separate route) open as new dockview tabs within the agent's workspace instead
* Each agent maintains its own dockview instance (hidden when not selected, shown when selected) to preserve DOM state across agent switches
* Layout changes are auto-saved with a 1-2 second debounce after the last change
* On page load, saved layouts are restored from the backend; if no saved layout exists, the default single-chat-tab layout is used
* Normal links in chat behave normally; `target=_blank` opens in the browser/new window; the programmatic API (`$llm.openTab(...)`) is used to open new dockview tabs
* Dockview's CSS variables are overridden to map to the existing dark theme variables, so tabs blend with the current design

## Changes

* Add `dockview-core` as a frontend dependency
* Create a new `DockviewWorkspace` component that wraps dockview initialization and manages per-agent instances (create on first agent selection, hide/show on switch)
* Create panel renderer functions for each tab type: `ChatPanel` (wraps existing `MessageList` + `MessageInput`), `IframePanel` (for Terminal and Custom URL), `SubagentPanel` (wraps existing `SubagentView`)
* Mount Mithril components into dockview panel containers using `m.mount()` in the `createComponent` callback
* Replace `App.ts` layout: remove the `app-content-wrapper` div (header + main + footer) and render `DockviewWorkspace` in its place
* Move header (agent name) from a standalone element into the dockview tab title
* Move `MessageInput` from a sibling footer into the `ChatPanel` component
* Add a "+" button to the dockview tab bar with a dropdown menu listing available tab types
* Add a `CustomUrlDialog` component (modal with URL + optional title fields) triggered by the "Custom URL" menu item
* Add `openTab` method to the `$llm` API (`LlmApi` interface) for programmatic tab creation by plugins
* Remove the `/agents/:agentId/subagents/:subagentSessionId` route from `index.ts`; subagent links in chat now call `$llm.openTab()` to open a `SubagentPanel` tab instead
* Add layout serialization/deserialization: serialize dockview layout + per-tab params (tab type, URL, subagent session ID) into a single JSON structure
* Add debounced auto-save: listen to dockview layout change events, debounce 1-2 seconds, then POST to backend
* Add two backend endpoints in `server.py`:
  - `GET /api/agents/{agent_id}/layout` -- reads layout JSON from `<agent_state_dir>/workspace_layout/layout.json`, returns it (or 404 if none saved)
  - `POST /api/agents/{agent_id}/layout` -- writes the request body as layout JSON to `<agent_state_dir>/workspace_layout/layout.json` (creates the directory if needed)
* Add dockview theme overrides in `style.css` mapping dockview CSS variables to the existing theme variables (`--color-bg-primary`, `--color-border`, etc.)
* Enforce Chat tab singleton: the "+" menu's Chat option focuses the existing chat tab rather than creating a new one
* Mark the Chat tab as unclosable via dockview's panel options
