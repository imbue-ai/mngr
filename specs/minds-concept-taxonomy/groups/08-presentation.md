# Group 8: Presentation

This taxonomy covers the presentation-layer concepts in the Minds / FCT codebase: how content is arranged into visual containers, how web services are surfaced as tabs, how terminals are created and surfaced, and what "browsers" means in practice.

All claims are grounded in actual code. Docs are used as hints only; divergences are flagged explicitly.

---

## Concept 1: Layout / Tabs / Panels

### 1.1 Canonical Definition

The workspace UI is a single **dockview** instance (`DockviewComponent` from `dockview-core`). Its state is called the **layout**. The layout is persisted to `$MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/layout.json` with a 1.5-second autosave debounce.

**File citations:**
- Layout schema root: `apps/system_interface/frontend/src/views/DockviewWorkspace.ts:101-104` — `SavedLayout = { dockview: SerializedDockview; panelParams: Record<string, PanelParams> }`
- Layout persistence path: `apps/system_interface/imbue/system_interface/server.py:580-581` — `get_host_dir() / "agents" / agent_id / "workspace_layout"`
- Layout save/load: `DockviewWorkspace.ts:973-1012`

### 1.2 The Three Structural Layers in Dockview

Dockview imposes three distinct structural layers. These have precise internal names that differ from how humans colloquially say things.

#### 1.2.1 Panel

A **panel** is the atomic addressable content unit. Each panel has:
- A unique **panel_id** (e.g. `chat-<uuid>`, `iframe-agent-<uuid>-<timestamp>`, `subagent-<agentId>-<sessionId>`)
- A **component** type (`"chat"`, `"iframe"`, or `"subagent"`)
- A **PanelParams** record stored client-side in `panelParams: Map<string, PanelParams>`
- A **ref** — a stable, type-prefixed address used by agents and scripts (e.g. `chat:alice`, `service:web`, `terminal:1a2b3c4d`)

**File citations:**
- Panel creation: `DockviewWorkspace.ts:461-473` (`addChatPanel`), `503-515` (`openIframeTab`), `882-908` (`openSubagentTab`)
- PanelParams type: `DockviewWorkspace.ts:72-85`
- Panel id formats: `DockviewWorkspace.ts:463` (`chat-${chatAgentId}`), `505` (`${panelType}-${primaryId}-${Date.now()}`), `709` (`iframe-agent-${agent.id}-${Date.now()}`)
- Server-side panel ref resolution: `layout_ops.py:223-276` (`_resolve_ref`)

#### 1.2.2 Group

A **group** is a set of tabs (panels) that share the same rectangular area and are switched between via a tab bar. At any moment, exactly one panel in a group is **active** (foregrounded). The layout grid is a tree of groups; branches split horizontally or vertically; leaves are groups.

**File citations:**
- Group as dockview concept: `DockviewWorkspace.ts:765-806` (`findSiblingGroupInDirection`) — iterates `dockview.groups`
- `activeGroup` in persisted layout: `layout_ops.py:407` — `dockview.get("activeGroup")`
- Serialized grid leaf (= group): `layout_ops.py:313-327` — leaf node contains `views` (list of panel_ids) and `activeView`

#### 1.2.3 Tab

A **tab** is the visual clickable label within a group's tab bar corresponding to one panel. In the code, "tab" and "panel" are interchangeable from the user perspective — every panel has exactly one tab in its group's tab bar. The codebase uses "tab" in UI-facing language and "panel" in programmatic/API language.

**File citations:**
- Custom tab renderer: `DockviewWorkspace.ts:164-280` (`createCustomTab`)
- "Add tab" button: `DockviewWorkspace.ts:385-446` (`createAddTabButton`)
- User-facing language: `manage-layout/SKILL.md` uses "tab" throughout; `layout.py` docstring says "Surface a service in the UI"

#### 1.2.4 Branch (layout tree node)

A **branch** in the serialized layout is an internal grid node. It has `arrangement: "row"` (children side by side) or `arrangement: "column"` (children stacked). The root orientation is stored at `dockview.grid.orientation` and child branches alternate.

**File citations:**
- Branch serialization: `layout_ops.py:328-342` (`_serialize_grid_node` branch case)
- Compact rendering: `layout.py:739-765` (`_format_tree_compact`)

### 1.3 Panel Types

The `panelType` field in `PanelParams` is the canonical discriminator:

| `panelType` | Component | Description | Example panel_id |
|---|---|---|---|
| `"chat"` | `ChatPanel` | Agent conversation view | `chat-<agentId>` |
| `"iframe"` | `IframePanel` or `AgentTerminalPanel` | Any iframe-embedded content | `iframe-agent-<id>-<ts>`, `iframe-terminal-<uuid>` |
| `"subagent"` | `SubagentView` | Harness-level subagent transcript | `subagent-<agentId>-<sessionId>` |

**File citations:**
- `PanelType` union: `DockviewWorkspace.ts:70` — `type PanelType = "chat" | "iframe" | "subagent"`
- Dispatch: `DockviewWorkspace.ts:1672-1714` (`createComponent` switch)

### 1.4 Ref System

Every panel has a **ref** — a stable, type-prefixed address. The ref is the canonical name used by agents, the `layout.py` script, and the `manage-layout` skill.

| Ref prefix | Meaning | Resolved by |
|---|---|---|
| `service:<name>` | Iframe for a registered workspace service | `layout_ops.py:250-251` + `DockviewWorkspace.ts:637-663` |
| `chat:<name>` | Chat panel for a named agent | `layout_ops.py:240-247` + `DockviewWorkspace.ts:665-686` |
| `chat-terminal:<name>` | Per-agent terminal singleton | `layout_ops.py:252-256` + `DockviewWorkspace.ts:688-720` |
| `terminal:<hash>` | Anonymous terminal (new on each creation) | `layout_ops.py:257-259` + `DockviewWorkspace.ts:614-635` |
| `url:<hash>` | Ad-hoc external URL tab | `layout_ops.py:260-261` + `DockviewWorkspace.ts:722-746` |
| `subagent:<session-id>` | Subagent panel | `layout_ops.py:249` + `DockviewWorkspace.ts:1086-1090` |

The hash is the first 8 hex chars of SHA-256 of the panel_id string.

**File citations:**
- `_resolve_ref`: `layout_ops.py:223-276`
- `resolveRefToPanelId` (frontend): `DockviewWorkspace.ts:1050-1102`
- `_short_hash`: `layout_ops.py:170-178`
- `allocate_terminal_panel_id` (server pre-mints for terminals): `layout_ops.py:181-194`

### 1.5 Layout Operations

The layout mutation surface is accessible to agents via `scripts/layout.py`. It POSTs to `POST /api/layout/broadcast` (loopback-only), which broadcasts a `layout_op` WebSocket message to the frontend.

**File citations:**
- Known ops: `layout_ops.py:38-53` — `list`, `inspect`, `open`, `focus`, `split`, `close`, `move`, `rename`, `maximize`, `restore`, `replace-url`, `refresh`
- Broadcast endpoint: `server.py:918-1033`
- Frontend handler: `DockviewWorkspace.ts:1127-1162` (`handleLayoutOp`)
- Mutex: `layout_ops.py:113-167` (`LayoutMutex`) — serializes mutating ops, TTL=0.5s, returns HTTP 409 on contention

### 1.6 Layout State Storage

- Client-side: `panelParams: Map<string, PanelParams>` in `DockviewWorkspace.ts:109`
- Persisted: `POST /api/layout` saves `{ dockview: SerializedDockview; panelParams: Record<string, PanelParams> }` with 1.5s debounce
- Disk path: `$MNGR_HOST_DIR/agents/<MNGR_AGENT_ID>/workspace_layout/layout.json`
- Server-read: `layout_ops.py:380-410` (`layout_inspect`)

### 1.7 Terminology Variants (tab vs panel vs group vs view vs window)

The codebase uses several terms for overlapping concepts:

- **panel**: The programmatic/server-side term for the atomic addressable content unit. Used in `layout_ops.py`, `panelParams`, `panel_id`, `panelType`, `dockview.addPanel(...)`.
- **tab**: The user-facing visual term for the same thing — specifically the tab bar entry. Used in `SKILL.md`, `manage-layout`, `build-web-service`. Also used in "Add tab button", "tab title", "tab-mates".
- **group**: The dockview concept for a set of panels sharing a rectangular pane with a tab bar. NOT called "group" in user documentation; invisible to agent skill docs.
- **view**: Used in two distinct senses: (a) the Electron `WebContentsView` (see `chromeView`, `contentView`, `sidebarView`, `modalView` in `electron/main.js`) — an OS-level rendering surface; (b) the dockview-internal term for a panel within a group's `views` array (e.g. `activeView` in persisted JSON at `layout_ops.py:399`).
- **window**: Used exclusively to refer to the OS-level Electron window (`BaseWindow` in `electron/main.js:1`). Never used for layout panes.

**Inconsistency**: The persisted JSON uses `"activeView"` (dockview-internal) at `layout_ops.py:315` and `"activeGroup"` at `layout_ops.py:407`. `layout_inspect` exposes `active_panel` in its output (`layout_ops.py:407`) mapping `activeGroup` -- a naming inconsistency: the thing labeled `activeGroup` in dockview's serialized JSON is re-exposed as `active_panel` in the inspect API, when `activeGroup` is actually a group ID not a panel ID.

**DOC/CODE DIVERGENCE**: `layout_ops.py:407` — `"active_panel": dockview.get("activeGroup")` — the field is named `active_panel` but its value is `activeGroup` from dockview's persisted JSON, which holds a **group id**, not a panel id. The comment in `manage-layout/SKILL.md` refers to `active_panel: 1` (a numeric group id) without clarifying this.

### 1.8 Manage-layout Skill vs layout.py

The `manage-layout` skill (`FCT:.agents/skills/manage-layout/SKILL.md`) is a higher-level human/agent-facing description of `scripts/layout.py`. The skill says "tabs" everywhere; `layout.py` says "panels" in its internals. The skill is accurate to the code.

### 1.9 Recommended Canonical Term

- Use **panel** for the atomic addressable content unit (both in API and documentation, since this is the term used in `panelParams`, `panel_id`, and dockview APIs).
- Use **group** for the container that holds one or more panels with a shared tab bar, when precision is needed.
- Use **tab** only in user-facing copy referring to the visual tab-bar entry.
- Avoid **view** for layout concepts (it is already taken by Electron's `WebContentsView`).
- Avoid **window** for anything in the layout (it means Electron OS window).

---

## Concept 2: Web Views / UI Services

### 2.1 What is a "Service" vs an "Application"?

**Service** and **application** are used interchangeably in the codebase for the same real-world thing. The distinction that exists is:

- `ServiceName` (`primitives.py:4`): a string type for the name of an entry in `runtime/applications.toml`
- `ApplicationEntry` (`models.py:69-73`): the Python model holding `{ name, url }` for an entry in `runtime/applications.toml`
- The URL segment is `/service/<name>/` — so "service" is the URL-level term
- The registry file is called `applications.toml` — so "application" is the registry-level term
- The Python model is `ApplicationEntry` and the WS message is `applications_updated` (`server.py:755-758`)
- The frontend model is `ApplicationEntry` (`AgentManager.ts:24-27`)

**File citations:**
- `ServiceName`: `primitives.py:4`
- `ApplicationEntry` model: `models.py:69-73`
- Registry file: `forward_port.py:20` — `DEFAULT_APPLICATIONS_FILE = "runtime/applications.toml"`
- URL routing: `service_dispatcher.py:398-408` — `/service/{service_name}/{path:path}`
- WS update: `server.py:755-758` — `"applications_updated"` with `"applications"`
- Frontend type: `AgentManager.ts:24-27` — `interface ApplicationEntry { name: string; url: string }`

**Inconsistency**: `AgentManager.ts:314-315` lists `"system_interface"` and `"terminal"` as application names to exclude from the add-tab dropdown. `layout_ops.py:98` has `_HIDDEN_SERVICES: frozenset[str] = frozenset({"system_interface"})` — only `system_interface` is hidden there (terminal IS shown in layout list). So "terminal" is treated as an application for routing but excluded from the UI dropdown as a named app tab (it's opened via "New terminal" instead). This inconsistency between the Python hidden list and the frontend filter is a minor but real divergence.

### 2.2 How a Service Becomes a UI Tab

The lifecycle is:

1. **Registration**: A service calls `forward_port.py --name <name> --url <url>` which upserts an entry into `runtime/applications.toml`
2. **Discovery**: `AgentManager` watches `runtime/applications.toml` and broadcasts `applications_updated` over WebSocket
3. **Frontend updates**: `AgentManager.ts` receives `applications_updated` and updates `applications: ApplicationEntry[]`
4. **Tab creation**: An agent calls `layout.py open <name>` or the user clicks the "+" dropdown. This calls `openIframeTab(proxyUrl, app.name, "iframe", app.name)` which calls `dockview.addPanel({ component: "iframe", ... })`
5. **Content delivery**: The `IframePanel` component renders `<iframe src="/service/<name>/">`. The `service_dispatcher.py` handles `GET /service/<name>/<path>` by proxying to the registered backend URL

**File citations:**
- `forward_port.py:62-78` — upsert into `applications.toml`
- `agent_manager.py:49` — `_APPLICATIONS_TOML_FILENAME = "runtime/applications.toml"`
- `DockviewWorkspace.ts:315-324` — dropdown builds links for registered apps
- `DockviewWorkspace.ts:49-51` — `getServiceUrl(serviceName) => /service/${serviceName}/`
- `service_dispatcher.py:217-273` — `_handle_service_http` proxies to backend
- `IframePanel.ts:13-30` — renders `<iframe src={url}>`

### 2.3 Service Worker and Proxying

First-navigation to `/service/<name>/` installs a **scoped Service Worker** (`__sw.js`) that rewrites all fetch requests from the service's own frontend to prepend `/service/<name>/`. This allows the proxied app to use relative paths internally.

**File citations:**
- Bootstrap HTML: `proxy.py:24-58` (`generate_bootstrap_html`)
- Service worker: `proxy.py:62-99` (`generate_service_worker_js`)
- URL rewriting: `proxy.py:102-132` (`generate_websocket_shim_js`) — shims WebSocket constructor
- Cookie scoping: `proxy.py:136-153` (`rewrite_cookie_path`)

### 2.4 The "system_interface" as a Hidden Service

`system_interface` is itself registered as an application (it's the workspace UI shell). It is excluded from agent-visible listings:

**File citations:**
- `layout_ops.py:98` — `_HIDDEN_SERVICES = frozenset({"system_interface"})`
- `DockviewWorkspace.ts:315` — also filtered: `app.name !== "system_interface"`

### 2.5 Terminology: Service vs Application vs Web View

| Term | Used where | Meaning |
|---|---|---|
| `service` | URL (`/service/<name>/`), ref prefix (`service:<name>`), `ServiceName` type | An entry in `applications.toml` exposed as an iframe tab via the proxy |
| `application` | `ApplicationEntry`, `applications.toml`, WS message `applications_updated` | Same thing — the registry-level name |
| "web view" | `build-web-service/SKILL.md` heading ("How to build a web service" + "A 'web service' here is something the user can click on as a tab") | Colloquial description, not a code term |
| `iframe` | `panelType: "iframe"`, `IframePanel`, `component: "iframe"` | The dockview panel/component type for any iframe-embedded content |

**Inconsistency**: The term "web view" is only used in the skill docs as a colloquial description; the actual code consistently uses `service`/`application`/`iframe`. There is no class or type named `WebView` anywhere.

**Inconsistency**: The Python `AgentManager` uses `list_service_names()` while the model is `ApplicationEntry`. `layout_ops.py:429` iterates `service_names` while `layout_list` returns `kind: "service"`. The dual naming (`service` for URL/ref, `application` for registry) is a persistent inconsistency.

### 2.6 Recommended Canonical Term

- **service** for the runtime-registered, URL-accessible, iframe-embedded workload (matches URL segment and ref prefix)
- **application** as an acceptable synonym in registry/model contexts (matches `applications.toml`)
- Avoid "web view" as a term in code — it is doc-only and colloquial
- **iframe panel** for the dockview panel type that hosts a service (or any URL)

---

## Concept 3: Terminals

### 3.1 What is a Terminal?

A **terminal** is an iframe panel that loads a URL served by a `ttyd` process. The ttyd process runs in a tmux window named `"terminal"` alongside each agent. The terminal iframe renders an interactive shell session via ttyd's web terminal UI.

### 3.2 How Terminals are Created

**Path 1: Anonymous terminal ("New terminal" button)**
1. The user clicks "New terminal" in the "+" dropdown
2. `openIframeTab(buildTerminalUrl(), "terminal")` is called (`DockviewWorkspace.ts:364-366`)
3. `buildTerminalUrl()` returns `/service/terminal/?arg=_&arg=workdir&arg=<work_dir>` (`DockviewWorkspace.ts:583-589`)
4. OR for agent-driven: `layout.py open terminal` → server calls `allocate_terminal_panel_id()` pre-minting a `terminal:<hash>` ref → broadcasts `open` op with `panel_id` hint → frontend uses the hint as the panel's id

**Path 2: Agent-attached terminal ("Open agent terminal" link)**
1. From the chat panel: `openAgentTerminalTab(agentId)` → `openIframeTabForAgent(agentId, url, title)` (`ChatPanel.ts:70-73`)
2. URL: `buildAgentTerminalUrl(agentName)` returns `/service/terminal/?arg=_&arg=agent&arg=<name>` (`DockviewWorkspace.ts:64-68`)
3. OR via ref: `layout.py open chat-terminal:<name>`
4. This opens an `AgentTerminalPanel` (not a plain `IframePanel`) which first POSTs to `/api/agents/<id>/start` before mounting the iframe

**File citations:**
- `buildTerminalUrl`: `DockviewWorkspace.ts:583-589`
- `buildAgentTerminalUrl`: `DockviewWorkspace.ts:64-68`
- `AgentTerminalPanel`: `views/AgentTerminalPanel.ts:1-92` — starts the agent first, then mounts IframePanel
- Discriminating agent vs workdir terminal: `DockviewWorkspace.ts:1689-1696` — `iframeUrl.includes("arg=agent")`
- `allocate_terminal_panel_id`: `layout_ops.py:181-194`

### 3.3 How ttyd Works

The `mngr_ttyd` plugin (`libs/mngr_ttyd/imbue/mngr_ttyd/plugin.py`) and the FCT-level `scripts/run_ttyd.sh` both provision ttyd. They differ slightly:

- **mngr_ttyd plugin** (`plugin.py:18-68`): Runs ttyd on a random port (`-p 0`), detects the assigned port from stdout, writes `ServiceLogRecord` events to `$MNGR_AGENT_STATE_DIR/events/services/events.jsonl`
- **FCT run_ttyd.sh** (`scripts/run_ttyd.sh`): Runs ttyd on fixed port 7681, registers via `forward_port.py --name terminal --url http://localhost:7681`

Both use **URL-arg dispatch** (`-a` flag, ttyd `--url-arg`): the first `?arg=` parameter is a dispatch KEY:
- No arg or `arg=_&arg=workdir&arg=<dir>`: opens bash in the specified directory
- `arg=_&arg=agent&arg=<name>`: runs `commands/ttyd/agent.sh <name>` which attaches to `<MNGR_PREFIX><name>:0` tmux session

**File citations:**
- ttyd command construction: `plugin.py:18-68` (`_build_ttyd_command`)
- URL-arg dispatch script: `run_ttyd.sh:17-30`
- Agent dispatch script: `run_ttyd.sh:38-54` (`agent.sh` inline) — `exec tmux attach -t "${MNGR_PREFIX:-mngr-}$1":0`
- `ttyd_agent.sh` resource: `libs/mngr_ttyd/imbue/mngr_ttyd/resources/ttyd_agent.sh`

### 3.4 Terminal Registration

The terminal service registers under the name `"terminal"` in `runtime/applications.toml`. This makes it accessible at `/service/terminal/`. The `system_interface` proxy routes `/service/terminal/...` to the ttyd backend.

**File citations:**
- Registration: `run_ttyd.sh:66` — `forward_port.py --name terminal --url "http://localhost:$TTYD_PORT"`
- `_TERMINAL_SERVICE_URL_PATH = "/service/terminal/"`: `layout_ops.py:35`
- Ref extraction from URL: `layout_ops.py:197-220` (`_extract_agent_terminal_name`)

### 3.5 Terminal Refs

| Ref | Meaning |
|---|---|
| `service:terminal` | The terminal service itself (used as a creation target in `open`/`split`) |
| `terminal:<hash>` | A specific anonymous terminal tab (addressable after creation) |
| `chat-terminal:<name>` | The per-agent terminal singleton (addressable by agent name) |

**File citations:**
- `layout_ops.py:35` — `_TERMINAL_SERVICE_URL_PATH`
- `layout_ops.py:252-256` — `chat-terminal:` ref resolution from URL shape
- `layout_ops.py:257-259` — `terminal:<hash>` for anonymous URLs starting with the terminal path
- `DockviewWorkspace.ts:613-635` — `service:terminal` creation bypasses dedup (always fresh)

### 3.6 Terminology Variants

- **terminal**: The general term, used consistently across all layers (service name, ref prefix, tmux window name, component)
- **terminal tab**: Colloquial, used in documentation. Same as a panel with `panelType: "iframe"` and URL pointing to `/service/terminal/`
- **agent terminal** / **chat-terminal**: The singleton terminal attached to a specific agent's tmux session
- **anonymous terminal**: A fresh bash session (no specific agent), created by "New terminal" or `open terminal`
- **ttyd**: The underlying binary; not user-facing

### 3.7 DOC/CODE DIVERGENCE

The `mngr_ttyd` plugin (`plugin.py`) is the mngr-level provisioning path; `run_ttyd.sh` is the FCT-level fallback. Both exist in this codebase. The `mngr_ttyd` plugin registers on a **random port** and uses `ServiceLogRecord` events; `run_ttyd.sh` uses **fixed port 7681** and calls `forward_port.py` directly. These are parallel implementations that could produce inconsistent behavior if both run.

---

## Concept 4: Browsers

### 4.1 What "Browsers" Means in the Concepts Doc

The `Minds_concepts.md` lists "browsers — tabs where you can have a (partially agent-controlled) browser" as a presentation concept. Investigation of the actual code reveals this is **not a distinct implemented concept** in the current codebase. There is no panel type, component, or API endpoint named "browser" or "BrowserPanel" in the dockview UI.

### 4.2 What Actually Exists Related to "Browser"

There are several distinct uses of the word "browser" in the codebase, none of which is the concept described in the concepts doc:

**4.2.1 The Electron desktop client is itself a "browser" (sort of)**

The Electron app (`electron/main.js`) renders the workspace UI using `WebContentsView` (Chromium-based). The `contentView` is an Electron `WebContentsView` that loads the system_interface URL. There is no separate "browser panel" — the entire workspace UI is rendered by Electron's web engine.

**File citations:**
- `electron/main.js:1` — imports `WebContentsView`
- `electron/main.js:426-444` — `contentView = new WebContentsView(...)` loads the workspace
- `electron/main.js:30-31` — comments mention "browser mode's iframe layout" (meaning the web browser access path)

**4.2.2 "Browser mode" vs "Electron mode"**

The system_interface can be accessed both via a real web browser (the user opens it in Chrome/Firefox) and via the Electron desktop client. Some code distinguishes these:

**File citations:**
- `templates.py:1086` — "In Electron mode, the iframe and browser sidebar are hidden via JS"
- `electron/main.js:30-31` — "Matches browser mode's iframe layout"

**4.2.3 Iframe panels as partial "browser tabs"**

The closest thing to an "agent-controlled browser" is an `iframe` panel pointed at an arbitrary URL (the `url:<hash>` ref type, created via "New URL" dialog or `open https://...`). Agents can control these via `replace-url` to navigate the iframe. However:
- This is just the `IframePanel` component with `panelType: "iframe"` and no `serviceName`
- It is NOT labeled "browser" anywhere in the code
- Agents cannot intercept network requests or interact with the iframe's DOM

**File citations:**
- `DockviewWorkspace.ts:722-746` — external URL tab creation, no `serviceName`
- `DockviewWorkspace.ts:910-971` — `showCustomUrlDialog()` ("New URL" menu item)
- `layout.py:_cmd_open` — `open https://example.com` creates an ad-hoc URL tab

**4.2.4 Latchkey browser auth**

"Browser" frequently appears in `latchkey/handlers/predefined.py` meaning the OAuth browser flow (`latchkey auth browser`) for credential acquisition. This is unrelated to the UI concept.

**4.2.5 Playwright browser for testing/automation**

`telegram/credential_extractor.py:41` and `desktop_client/e2e_workspace_runner.py:577` use Playwright's `Browser` object for automation/testing.

### 4.3 Assessment

The "browsers — tabs where you can have a (partially agent-controlled) browser" concept described in `Minds_concepts.md` is a **future/aspirational concept** that is not implemented in the current codebase as a distinct entity. What currently exists is:

1. **Ad-hoc URL panels** (`url:<hash>` ref) — iframes pointed at external URLs, can be navigated by agents via `replace-url`, but with full iframe sandbox restrictions
2. **Service iframe panels** (`service:<name>`) — proxied service tabs
3. No dedicated "browser panel" type, no DOM interaction from agents, no network-request interception

**DOC/CODE DIVERGENCE**: `Minds_concepts.md:77` — "browsers — tabs where you can have a (partially agent-controlled) browser" — this is listed as an existing concept but no such dedicated concept exists in the code. The closest existing feature is the ad-hoc URL iframe tab (`url:<hash>`), which the agent can navigate via `replace-url` but cannot control in any deeper browser sense.

---

## Concept 5: Workspace Color / Accent

### 5.1 Canonical Definition

A **workspace color** (surfaced in the UI as the workspace's **accent**) is a per-workspace `#rrggbb` hex chosen by the user. It is stored as the `color` **label** on the primary workspace's mngr agent and drives the titlebar background, the sidebar workspace-row dot, the homepage tile, the inbox card accent, and the `--workspace-accent` / `--titlebar-bg` / `--titlebar-fg` CSS variables. Unlike the layout/terminal/service concepts (which live in the FCT `system_interface`), this is a **Minds-app concept**: it lives entirely in `apps/minds/imbue/minds/desktop_client/`.

**File citations:**
- Palette + pure helpers: `apps/minds/imbue/minds/desktop_client/workspace_color.py` — `WORKSPACE_PALETTE` (12 colors, `workspace_color.py:43-56`), `DEFAULT_WORKSPACE_COLOR_NAME = "confusion"` (`workspace_color.py:63-64`)
- Stored as the `color` agent label: `backend_resolver.py:809-838` (`get_workspace_color` reads `agent.labels.get("color")` and normalizes), `backend_resolver.py:840-862` (`set_workspace_color_locally`)
- Resolution with default fallback: `app.py:420-430` (`_resolved_workspace_color`)

### 5.2 The Palette

The palette is **server-side only** (`WORKSPACE_PALETTE` in `workspace_color.py`): 11 named entries sourced from a Figma node (`356:4113`) plus a literal `#ffffff` white. Names are kebab-case (`confusion`, `courage`, `envy`, `peace`, `belonging`, `energy`, `strength`, `comfort`, `inspiration`, `clarity`, `indifference`, `white`) and are not shown visually — the picker renders unlabeled swatches, and the names are used only as `ColorSwatch` `aria-label`s and to name the default. The 10 chromatic entries come first; the two achromatic neutrals (`indifference` = black, `white`) are grouped last so `pick_unused_create_color` hands out a real color before the neutrals.

There is intentionally **no JS palette mirror**: the swatches are server-rendered and carry `data-color` attributes that the picker JS reads. A guard test (`templates_test.py`) asserts the JS never reintroduces a palette mirror and keeps exporting only the two pure helpers.

**File citations:**
- Palette + default: `workspace_color.py:43-64`
- Create-form preselect logic: `workspace_color.py:76-97` (`pick_unused_create_color`)
- Server-rendered swatches: `templates/ColorSwatch.jinja` (`role="radio"`, `data-color`, `aria-label`)

### 5.3 The Two Pure Helpers (Python + JS mirror)

Two pure functions are mirrored between Python (`workspace_color.py`) and JS (`static/workspace_accent.js`, exposed as `window.mindsAccent`) so the picker pages can validate input and preview the titlebar foreground locally without a server round-trip:

| Python (`workspace_color.py`) | JS (`workspace_accent.js`) | Purpose |
|---|---|---|
| `normalize_workspace_color` (`101-117`) | `normalizeHex` | Lenient hex parser: accepts `#fff` / `fff` / `#ffffff` / `ffffff` (any case, whitespace tolerated), returns canonical `#rrggbb` lowercase or `None`. Alpha (`#rrggbbaa`) is rejected. |
| `pick_workspace_foreground` (`130-146`) | `pickForegroundForHex` | WCAG-luminance contrast picker: returns `"0 0 0"` or `"255 255 255"` for the titlebar foreground (`--titlebar-fg`), thresholded at relative luminance `0.179`. |

**File citations:**
- Python helpers: `workspace_color.py:100-146`
- JS mirror: `static/workspace_accent.js:19-57` (`window.mindsAccent = { normalizeHex, pickForegroundForHex }`)

### 5.4 How a Color is Set

There are two write paths, both ultimately writing the `color` label:

1. **At create time**: the create form posts a hidden `color` input (the picker's selected swatch). `_color_for_new_workspace` (`app.py:432-450`) leniently parses it (malformed or missing → default), and the value is threaded into agent creation. `_suggested_create_color` (`app.py:453-464`) preselects the first unused palette entry.
2. **Via workspace settings**: the settings page renders the picker (12 swatches + an always-visible hex input; selecting a swatch saves immediately, no Save button). `static/workspace_settings.js` POSTs `{"hex": "<rrggbb>"}` to `POST /api/workspaces/<agent_id>/color`, handled by `_handle_set_workspace_color_api` (`app.py:1864-1979`), which writes `color=<hex>` via `mngr label` (CLI merge semantics, preserving other labels) and optimistically updates the resolver snapshot so the next SSE `workspaces` tick reflects the new color.

**File citations:**
- Settings POST endpoint: `app.py:1864-1979` (`_handle_set_workspace_color_api`), registered at `app.py:4465` (`POST /api/workspaces/{agent_id}/color`)
- Create-form parse: `app.py:432-450` (`_color_for_new_workspace`), `app.py:453-464` (`_suggested_create_color`)
- Settings picker JS: `static/workspace_settings.js:20-225`
- `mngr label` write: `app.py:1957` — `[mngr_binary, "label", str(parsed_id), "-l", f"color={normalized}"]`

The settings endpoint returns distinct error discriminants: `400 invalid_hex`, `404 not_primary` (not a primary workspace), `409 stale_provider` (provider's last discovery poll errored), `502 host_unreachable` (`mngr label` failed). Color writes apply only to **primary** workspaces (`workspace` + `is_primary` label pair).

### 5.5 How a Color Reaches the UI

The resolved accent is attached to each entry in the SSE `workspaces` payload as `accent` (`#rrggbb`) and `accent_fg` (the contrasting RGB triple). `chrome.js` / `sidebar.js` drop those into CSS variables; `workspace_color.py` is never imported client-side.

**File citations:**
- SSE payload build: `app.py:2508-2549` (`_build_workspace_list`) — `accent = _resolved_workspace_color(...)`, `accent_fg = pick_workspace_foreground(accent)`
- Titlebar application: `static/chrome.js:101-176` — sets `--workspace-accent`, `--titlebar-bg`, `--titlebar-fg` on the document root per active workspace
- Sidebar dot: `static/sidebar.js` + the `.sidebar-dot` token (per-workspace accent circle, colored inline per workspace)
- Inbox card accent: `app.py:3663-3680` — mirrors the homepage tile's accent off the homepage agent's id
- CSS variables + tokens: `static/tokens.css` (`--workspace-accent` / `--titlebar-bg` / `--titlebar-fg`, `.color-swatch`, `.color-hex-pill`, `.accent-spine`, `.sidebar-dot`)

### 5.6 Default / Backfill Behavior

Workspaces created before the picker shipped have **no** `color` label. `_resolved_workspace_color` (`app.py:420-430`) returns `DEFAULT_WORKSPACE_COLOR` (`confusion`, `#0b292b`) for them; nothing proactively backfills the label — a pre-picker workspace renders with the default until the user explicitly picks a color.

### 5.7 ColorSwatch Component and DevStyleguide

The picker swatch is a reusable JinjaX primitive, `ColorSwatch` (`templates/ColorSwatch.jinja`): a circular `role="radio"` button owning the markup contract the picker JS selects on (`.color-swatch`, `aria-checked`, `data-color`, `aria-label`). It is used by both the settings picker (`size="md"`, 34px) and the create-form picker (`size="sm"`, 24px). The live component catalog `DevStyleguide` (`templates/pages/DevStyleguide.jinja`, mounted at `/_dev/styleguide`) demos it alongside the other form-control primitives.

**File citations:**
- Swatch component: `templates/ColorSwatch.jinja`
- Catalog: `templates/pages/DevStyleguide.jinja`, README entry: `templates/README.md:52`

### 5.8 Terminology Variants

- **color**: The storage-level term — the `color` agent label, the `color` form/JSON field, the `WORKSPACE_PALETTE` keys' values. Used in `workspace_color.py`, the create/settings forms, and the label write.
- **accent**: The UI-level term — `accent` / `accent_fg` in the SSE payload, `--workspace-accent`, `.accent-spine`, `static/workspace_accent.js`, `accentByAgentId` in `chrome.js`. The same hex, framed as the visible workspace accent.
- **palette**: The fixed server-side set of 12 pickable colors (`WORKSPACE_PALETTE`); a user may also type any custom hex, which is not a palette entry.
- **swatch**: The circular radio control rendering one color (`ColorSwatch.jinja`, `.color-swatch`).

**Inconsistency**: The concept is named **color** at the storage/form layer (`color` label, `color` field, `workspace_color.py`) but **accent** at the rendering layer (`accent` SSE field, `--workspace-accent`, `workspace_accent.js`). They refer to the same per-workspace hex; the dual naming parallels the service/application split in Concept 2.

### 5.9 Recommended Canonical Term

- Use **workspace color** for the user-chosen value as stored/configured (matches the `color` label and `workspace_color.py`).
- Use **accent** for the same value as it paints the chrome (matches the SSE `accent` field and the `--workspace-accent` CSS variable).
- Reserve **palette** for the fixed server-side set of 12 pickable colors, distinct from a custom hex.

---

## Cross-Cutting Inconsistencies Summary

### Inconsistency A: Panel vs Tab vs View vs Window

- **panel** = atomic content unit (programmatic, server-side, dockview API)
- **tab** = the visual tab-bar entry (user-facing, doc-level)
- **view** = overloaded: (1) dockview-internal for panel within a group's `views` array; (2) Electron `WebContentsView` (OS-level rendering surface)
- **window** = Electron `BaseWindow` (OS window) — never layout pane
- **group** = a pane holding multiple panels with a tab bar — not user-visible in docs

The field `active_panel` in `layout_inspect` output actually contains an `activeGroup` id (a group id, not a panel id). This is a naming bug at `layout_ops.py:407`.

### Inconsistency B: Service vs Application

- The URL prefix is `/service/<name>/` — so "service" is the URL-level term
- The registry is `runtime/applications.toml`, the Python model is `ApplicationEntry`, and the WS event is `applications_updated` — so "application" is the registry/model term
- These refer to the same concept. No single canonical term is used consistently across all layers.

### Inconsistency C: "Browser" as an Aspirational vs Implemented Concept

The `Minds_concepts.md` lists "browsers" as an existing concept in the "Presentation" section, but there is no implemented "browser panel" type. The closest implementation is the ad-hoc URL iframe tab (`url:<hash>`), which allows limited agent navigation via `replace-url`.

### Inconsistency D: terminal hidden from applications listing but not from layout list

`layout_ops.py:98` hides only `system_interface` from `layout_list`. `DockviewWorkspace.ts:315` also hides `terminal` from the add-tab dropdown's "applications" section (because terminals are opened via the dedicated "New terminal" item). But `layout_list` does return the `service:terminal` entry, so agents can see it and interact with it via `layout.py`. This asymmetry between what agents and the UI show is potentially confusing.

### Inconsistency E: Two ttyd provisioning paths

Both `libs/mngr_ttyd/plugin.py` (random port, ServiceLogRecord events) and `scripts/run_ttyd.sh` (fixed port 7681, `forward_port.py`) provision ttyd. These are parallel, non-equivalent implementations. In a mngr-provisioned environment, the plugin runs; in an FCT-bootstrapped environment, `run_ttyd.sh` runs. Both are in the same repo scope.

### Inconsistency F: Workspace color vs accent

The per-workspace color is named **color** at the storage/form layer (the `color` agent label, the `color` form/JSON field, `workspace_color.py`) but **accent** at the rendering layer (the `accent` / `accent_fg` SSE fields, `--workspace-accent`, `static/workspace_accent.js`). They are the same `#rrggbb` value. This is a Minds-app concept (`apps/minds/imbue/minds/desktop_client/`), distinct from the FCT-grounded layout/terminal/service concepts above, and the color/accent split mirrors the service/application split in Concept 2.
