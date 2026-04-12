/**
 * Dockview-based tabbed workspace for the main content area.
 * Manages one DockviewComponent per agent, hiding/showing as agents are selected.
 */

import m from "mithril";
import {
  DockviewComponent,
  themeLight,
  type IContentRenderer,
  type IHeaderActionsRenderer,
  type SerializedDockview,
} from "dockview-core";
import { ChatPanel } from "./ChatPanel";
import { IframePanel } from "./IframePanel";
import { SubagentView } from "./SubagentView";
import { CreateAgentModal } from "./CreateAgentModal";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";
import { ShareModal } from "./ShareModal";
import { apiUrl, getPrimaryAgentId } from "../base-path";
import {
  getAgentById,
  getChatAgentsForParent,
  getApplicationsForAgent,
  getChatProtoAgentsForParent,
  getSidebarAgents,
} from "../models/AgentManager";
import { selectAgent } from "../navigation";

const AUTOSAVE_DEBOUNCE_MS = 1500;

type AccessMode = "cloudflare" | "local" | "dev";

function getAccessMode(): AccessMode {
  const hostname = window.location.hostname;
  if (hostname.match(/^[^-]+--(.*)/)) {
    return "cloudflare";
  }
  if (window.location.pathname.match(/^.*\/agents\/[^/]+\//)) {
    return "local";
  }
  return "dev";
}

// SVG path constants for tab action icons
const SVG_CLOSE = '<line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/>';
const SVG_TRASH = '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>';
const SVG_SHARE = '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>';

function getApplicationUrl(appName: string, rawUrl: string, _agentId: string): string {
  const hostname = window.location.hostname;

  // Cloudflare proxy: server--agentid--username.domain
  const cfMatch = hostname.match(/^[^-]+--(.*)/);
  if (cfMatch) {
    const proto = window.location.protocol;
    const port = window.location.port ? `:${window.location.port}` : "";
    return `${proto}//${appName}--${cfMatch[1]}${port}/`;
  }

  // Local forwarding server: /agents/{id}/{server_name}/
  const pathMatch = window.location.pathname.match(/^(.*\/agents\/[^/]+)\//);
  if (pathMatch) {
    return `${pathMatch[1]}/${appName}/`;
  }

  // Dev mode (no forwarding server): use the raw URL from applications.toml
  return rawUrl;
}

function getTerminalUrl(): string {
  const hostname = window.location.hostname;

  // Cloudflare proxy: terminal--agentid--username.domain
  const cfMatch = hostname.match(/^[^-]+--(.*)/);
  if (cfMatch) {
    const proto = window.location.protocol;
    const port = window.location.port ? `:${window.location.port}` : "";
    return `${proto}//terminal--${cfMatch[1]}${port}/`;
  }

  // Local forwarding server: always use the primary agent's terminal
  const primaryId = getPrimaryAgentId();
  const pathMatch = window.location.pathname.match(/^(.*\/agents\/)[^/]+\//);
  if (pathMatch && primaryId) {
    return `${pathMatch[1]}${primaryId}/terminal/`;
  }

  // Dev mode
  return "http://localhost:7681";
}

type PanelType = "chat" | "iframe" | "subagent";

interface PanelParams {
  panelType: PanelType;
  agentId: string;
  chatAgentId?: string;
  url?: string;
  title?: string;
  subagentSessionId?: string;
}

let showNewChatModal = false;
let newChatParentAgentId: string | null = null;

// Destroy dialog state
let showDestroyDialog = false;
let destroyTargetAgentId: string | null = null;
let destroyTargetAgentName: string | null = null;
let destroyTargetPanelId: string | null = null;
let destroyTargetDockviewAgentId: string | null = null;

// Share modal state
let showShareModal = false;
let shareServerName: string | null = null;

interface SavedLayout {
  dockview: SerializedDockview;
  panelParams: Record<string, PanelParams>;
}

interface AgentDockviewState {
  component: DockviewComponent;
  container: HTMLElement;
  panelParams: Map<string, PanelParams>;
  saveTimer: ReturnType<typeof setTimeout> | null;
  layoutChangeDisposable: { dispose: () => void } | null;
}

const agentDockviews: Map<string, AgentDockviewState> = new Map();
let currentAgentId: string | null = null;
let wrapperElement: HTMLElement | null = null;

function createMithrilRenderer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: m.ComponentTypes<any, any>,
  attrs: Record<string, unknown>,
): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.display = "flex";
  element.style.flexDirection = "column";

  return {
    element,
    init() {
      m.mount(element, { view: () => m(component, attrs) });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

function makeSvgIcon(pathContent: string, viewBox: string = "0 0 24 24"): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${pathContent}</svg>`;
}

function createTabActionButton(
  title: string,
  svgPath: string,
  onClick: (ev: MouseEvent) => void,
  className: string = "",
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.className = `dv-custom-tab-action ${className}`.trim();
  btn.title = title;
  btn.innerHTML = makeSvgIcon(svgPath);
  btn.addEventListener("pointerdown", (ev) => ev.preventDefault());
  btn.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick(ev);
  });
  return btn;
}

interface CustomTabOptions {
  panelParams: Map<string, PanelParams>;
  dockviewAgentId: string;
}

function createCustomTab(
  options: { id: string; name: string },
  tabOptions: CustomTabOptions,
): { element: HTMLElement; init: (params: { title: string; api: { close: () => void; onDidTitleChange: (cb: (e: { title: string }) => void) => { dispose: () => void }; isActive: boolean; onDidActiveChange: (cb: (e: { isActive: boolean }) => void) => { dispose: () => void } } }) => void; dispose: () => void } {
  const element = document.createElement("div");
  element.className = "dv-default-tab dv-custom-tab";

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  element.appendChild(content);

  const actions = document.createElement("div");
  actions.className = "dv-custom-tab-actions";
  actions.style.display = "none";
  element.appendChild(actions);

  const disposables: Array<{ dispose: () => void }> = [];

  return {
    element,
    init(params) {
      content.textContent = params.title ?? "";
      disposables.push(
        params.api.onDidTitleChange((event) => {
          content.textContent = event.title ?? "";
        }),
      );

      const panelParams = tabOptions.panelParams.get(options.id);
      const panelType = panelParams?.panelType ?? "chat";

      // Share button -- only on iframe/application tabs
      if (panelType === "iframe") {
        const serverName = panelParams?.title ?? "web";
        actions.appendChild(
          createTabActionButton("Share", SVG_SHARE, () => {
            shareServerName = serverName;
            showShareModal = true;
            m.redraw();
          }),
        );
      }

      // Destroy button -- only on chat/agent tabs
      if (panelType === "chat") {
        const chatAgentId = panelParams?.chatAgentId ?? panelParams?.agentId ?? tabOptions.dockviewAgentId;
        const primaryAgentId = getPrimaryAgentId();
        const isPrimary = chatAgentId === primaryAgentId;

        const destroyBtn = createTabActionButton(
          isPrimary ? "Cannot destroy the primary agent" : "Destroy agent",
          SVG_TRASH,
          () => {
            if (isPrimary) return;
            const agent = getAgentById(chatAgentId);
            destroyTargetAgentId = chatAgentId;
            destroyTargetAgentName = agent?.name ?? chatAgentId;
            destroyTargetPanelId = options.id;
            destroyTargetDockviewAgentId = tabOptions.dockviewAgentId;
            showDestroyDialog = true;
            m.redraw();
          },
          isPrimary ? "dv-custom-tab-action-disabled" : "dv-custom-tab-action-destructive",
        );
        if (isPrimary) {
          destroyBtn.disabled = true;
        }
        actions.appendChild(destroyBtn);
      }

      // Close button -- on all tab types
      actions.appendChild(
        createTabActionButton("Close tab", SVG_CLOSE, () => {
          params.api.close();
        }),
      );

      // Show/hide actions based on active state
      function updateActionsVisibility(isActive: boolean): void {
        actions.style.display = isActive ? "flex" : "none";
      }
      updateActionsVisibility(params.api.isActive);
      disposables.push(
        params.api.onDidActiveChange((event) => {
          updateActionsVisibility(event.isActive);
        }),
      );
    },
    dispose() {
      for (const d of disposables) {
        d.dispose();
      }
      disposables.length = 0;
    },
  };
}

function buildDropdownItems(
  agentId: string,
  dockviewState: AgentDockviewState,
): Array<{ label: string; action: () => void; dividerAfter?: boolean; header?: boolean }> {
  const items: Array<{ label: string; action: () => void; dividerAfter?: boolean; header?: boolean }> = [];

  // --- Chat section ---
  items.push({ label: "Chat", action: () => {}, header: true });

  const selectedAgent = getAgentById(agentId);
  if (selectedAgent) {
    items.push({
      label: selectedAgent.name,
      action: () => focusOrCreateChatPanelForAgent(agentId, agentId, selectedAgent.name, dockviewState),
    });
  }
  const chatAgents = getChatAgentsForParent(agentId);
  for (const chatAgent of chatAgents) {
    items.push({
      label: chatAgent.name,
      action: () => focusOrCreateChatPanelForAgent(agentId, chatAgent.id, chatAgent.name, dockviewState),
    });
  }
  const chatProtos = getChatProtoAgentsForParent(agentId);
  for (const proto of chatProtos) {
    items.push({
      label: `${proto.name} (creating...)`,
      action: () => focusOrCreateChatPanelForAgent(agentId, proto.agent_id, proto.name, dockviewState),
    });
  }

  items.push({
    label: "+ new chat",
    action: () => {
      showNewChatModal = true;
      newChatParentAgentId = agentId;
      m.redraw();
    },
    dividerAfter: true,
  });

  // --- Applications section ---
  items.push({ label: "Applications", action: () => {}, header: true });

  // Always show a terminal option. Uses the primary agent's ttyd instance
  // with a workdir dispatch to cd into the selected agent's work directory.
  // The terminal URL always routes through the primary agent since ttyd only
  // runs there -- we can't use getApplicationUrl because in local mode it
  // derives the agent ID from the current URL path (which points to the
  // selected agent, not the primary one).
  const currentAgent = getAgentById(agentId);
  const terminalBaseUrl = getTerminalUrl();
  // ttyd runs `bash -c "$DISPATCH_SCRIPT" <args...>`. In bash -c, the first
  // arg after the script becomes $0 (not $1). The dispatch script reads $1,
  // so we prepend a dummy arg for $0.
  const terminalUrl = currentAgent?.work_dir
    ? `${terminalBaseUrl}?arg=_&arg=workdir&arg=${encodeURIComponent(currentAgent.work_dir)}`
    : terminalBaseUrl;
  items.push({
    label: "terminal",
    action: () => openIframeTab(agentId, dockviewState, terminalUrl, "terminal"),
  });

  const apps = getApplicationsForAgent(agentId).filter((app) => app.name !== "web" && app.name !== "terminal");
  for (const app of apps) {
    const proxyUrl = getApplicationUrl(app.name, app.url, agentId);
    items.push({
      label: app.name,
      action: () => openIframeTab(agentId, dockviewState, proxyUrl, app.name),
    });
  }

  items.push({
    label: "+ custom URL",
    action: () => showCustomUrlDialog(agentId, dockviewState),
  });

  return items;
}

function createAddTabButton(agentId: string, dockviewState: AgentDockviewState): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dockview-add-tab-wrapper";

  const button = document.createElement("button");
  button.className = "dockview-add-tab-button";
  button.title = "Add tab";
  button.textContent = "+";
  element.appendChild(button);

  const dropdown = document.createElement("div");
  dropdown.className = "dockview-add-tab-dropdown";
  dropdown.style.display = "none";

  element.appendChild(dropdown);

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const isVisible = dropdown.style.display !== "none";
    if (isVisible) {
      dropdown.style.display = "none";
    } else {
      dropdown.innerHTML = "";
      const items = buildDropdownItems(agentId, dockviewState);
      for (const item of items) {
        if (item.header) {
          const header = document.createElement("div");
          header.className = "dockview-add-tab-dropdown-header";
          header.textContent = item.label;
          header.style.cssText = "padding: 4px 12px; font-size: 0.75em; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em;";
          dropdown.appendChild(header);
          continue;
        }

        const menuItem = document.createElement("div");
        menuItem.className = "dockview-add-tab-dropdown-item";
        menuItem.textContent = item.label;
        menuItem.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          dropdown.style.display = "none";
          item.action();
        });
        dropdown.appendChild(menuItem);

        if (item.dividerAfter) {
          const divider = document.createElement("div");
          divider.className = "dockview-add-tab-dropdown-divider";
          divider.style.borderTop = "1px solid #e5e7eb";
          divider.style.margin = "4px 0";
          dropdown.appendChild(divider);
        }
      }
      dropdown.style.display = "block";
    }
  });

  const closeDropdown = (e: MouseEvent) => {
    if (!element.contains(e.target as Node)) {
      dropdown.style.display = "none";
    }
  };
  document.addEventListener("click", closeDropdown);

  return {
    element,
    init() {},
    dispose() {
      document.removeEventListener("click", closeDropdown);
    },
  };
}

function focusOrCreateChatPanelForAgent(
  agentId: string,
  chatAgentId: string,
  chatAgentName: string,
  state: AgentDockviewState,
): void {
  const panelId = `chat-${chatAgentId}`;
  const existingPanel = state.component.panels.find((p) => p.id === panelId);
  if (existingPanel) {
    if (!existingPanel.api.isActive) {
      state.component.setActivePanel(existingPanel);
    }
    return;
  }
  addChatPanel(agentId, chatAgentId, chatAgentName, state);
}

function addChatPanel(
  agentId: string,
  chatAgentId: string,
  chatAgentName: string,
  state: AgentDockviewState,
): void {
  const panelId = `chat-${chatAgentId}`;
  const title = chatAgentName;
  const params: PanelParams = { panelType: "chat", agentId, chatAgentId };
  state.panelParams.set(panelId, params);
  state.component.addPanel({
    id: panelId,
    component: "chat",
    title,
    params,
    renderer: "always",
  });
}


function openIframeTab(
  agentId: string,
  state: AgentDockviewState,
  url: string,
  title: string,
  panelType: PanelType = "iframe",
): void {
  const panelId = `${panelType}-${agentId}-${Date.now()}`;
  const params: PanelParams = { panelType, agentId, url, title };
  state.panelParams.set(panelId, params);
  state.component.addPanel({
    id: panelId,
    component: "iframe",
    title,
    params,
  });
}

export function openIframeTabForAgent(agentId: string, url: string, title: string): void {
  const state = agentDockviews.get(agentId);
  if (!state) return;
  openIframeTab(agentId, state, url, title);
}

export function openSubagentTab(agentId: string, subagentSessionId: string, description: string): void {
  const state = agentDockviews.get(agentId);
  if (!state) return;

  const existingPanel = state.component.panels.find((p) => {
    const params = state.panelParams.get(p.id);
    return params?.panelType === "subagent" && params.subagentSessionId === subagentSessionId;
  });
  if (existingPanel) {
    state.component.setActivePanel(existingPanel);
    return;
  }

  const panelId = `subagent-${agentId}-${subagentSessionId}`;
  const params: PanelParams = {
    panelType: "subagent",
    agentId,
    subagentSessionId,
    title: description,
  };
  state.panelParams.set(panelId, params);
  state.component.addPanel({
    id: panelId,
    component: "subagent",
    title: description,
    params,
  });
}

function showCustomUrlDialog(agentId: string, state: AgentDockviewState): void {
  const overlay = document.createElement("div");
  overlay.className = "custom-url-dialog-overlay";

  const dialog = document.createElement("div");
  dialog.className = "custom-url-dialog";

  dialog.innerHTML = `
    <h3 class="custom-url-dialog-title">Open Custom URL</h3>
    <label class="custom-url-dialog-label">URL</label>
    <input type="url" class="custom-url-dialog-input" placeholder="https://example.com" autofocus />
    <label class="custom-url-dialog-label">Title (optional)</label>
    <input type="text" class="custom-url-dialog-input" placeholder="Tab title" />
    <div class="custom-url-dialog-actions">
      <button class="custom-url-dialog-cancel">Cancel</button>
      <button class="custom-url-dialog-open">Open</button>
    </div>
  `;

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);

  const inputs = dialog.querySelectorAll("input");
  const urlInput = inputs[0] as HTMLInputElement;
  const titleInput = inputs[1] as HTMLInputElement;

  function close(): void {
    document.body.removeChild(overlay);
  }

  function open(): void {
    const url = urlInput.value.trim();
    if (!url) return;

    let title = titleInput.value.trim();
    if (!title) {
      try {
        title = new URL(url).hostname;
      } catch {
        title = url;
      }
    }
    close();
    openIframeTab(agentId, state, url, title);
  }

  dialog.querySelector(".custom-url-dialog-cancel")!.addEventListener("click", close);
  dialog.querySelector(".custom-url-dialog-open")!.addEventListener("click", open);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  urlInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
    if (e.key === "Escape") close();
  });
  titleInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
    if (e.key === "Escape") close();
  });

  urlInput.focus();
}

async function saveLayout(agentId: string, state: AgentDockviewState): Promise<void> {
  const dockviewJson = state.component.toJSON();
  const panelParams: Record<string, PanelParams> = {};
  for (const [id, params] of state.panelParams) {
    panelParams[id] = params;
  }
  const payload: SavedLayout = { dockview: dockviewJson, panelParams };
  const mode = getAccessMode();

  try {
    await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/layout?mode=${mode}`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    // Layout save is best-effort
  }
}

function scheduleSave(agentId: string, state: AgentDockviewState): void {
  if (state.saveTimer !== null) {
    clearTimeout(state.saveTimer);
  }
  state.saveTimer = setTimeout(() => {
    state.saveTimer = null;
    saveLayout(agentId, state);
  }, AUTOSAVE_DEBOUNCE_MS);
}

async function loadLayout(agentId: string): Promise<SavedLayout | null> {
  const mode = getAccessMode();
  try {
    const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/layout?mode=${mode}`));
    if (!response.ok) return null;
    return (await response.json()) as SavedLayout;
  } catch {
    return null;
  }
}

function createDockviewForAgent(agentId: string, parentElement: HTMLElement): AgentDockviewState {
  const container = document.createElement("div");
  container.className = "dockview-agent-container dockview-theme-light";
  container.style.width = "100%";
  container.style.height = "100%";
  parentElement.appendChild(container);

  const panelParams = new Map<string, PanelParams>();

  const state: AgentDockviewState = {
    component: null as unknown as DockviewComponent,
    container,
    panelParams,
    saveTimer: null,
    layoutChangeDisposable: null,
  };

  const dockview = new DockviewComponent(container, {
    theme: themeLight,
    defaultRenderer: "always",
    defaultTabComponent: "custom",
    createComponent(options) {
      const params = (options as unknown as { params?: PanelParams }).params ?? panelParams.get(options.id);

      switch (options.name) {
        case "chat":
          return createMithrilRenderer(ChatPanel, {
            agentId: params?.chatAgentId ?? params?.agentId ?? agentId,
          });

        case "iframe":
          return createMithrilRenderer(IframePanel, {
            url: params?.url ?? "",
            title: params?.title ?? "Tab",
          });

        case "subagent":
          return createMithrilRenderer(SubagentView, {
            agentId: params?.agentId ?? agentId,
            subagentSessionId: params?.subagentSessionId ?? "",
          });

        default:
          return createMithrilRenderer(ChatPanel, { agentId });
      }
    },
    createTabComponent(options) {
      return createCustomTab(options, { panelParams, dockviewAgentId: agentId });
    },
    createLeftHeaderActionComponent() {
      return createAddTabButton(agentId, state);
    },
  });

  state.component = dockview;

  // Listen for layout changes and auto-save
  state.layoutChangeDisposable = dockview.api.onDidLayoutChange(() => {
    scheduleSave(agentId, state);
  });

  // Listen for panel removal to clean up params
  dockview.api.onDidRemovePanel((panel) => {
    panelParams.delete(panel.id);
  });

  return state;
}

async function initializeAgentDockview(agentId: string, parentElement: HTMLElement): Promise<void> {
  const state = createDockviewForAgent(agentId, parentElement);
  agentDockviews.set(agentId, state);

  const saved = await loadLayout(agentId);

  if (saved) {
    for (const [id, params] of Object.entries(saved.panelParams)) {
      state.panelParams.set(id, params);
    }
    try {
      state.component.fromJSON(saved.dockview);
      return;
    } catch {
      state.panelParams.clear();
    }
  }

  const agent = getAgentById(agentId);
  addChatPanel(agentId, agentId, agent?.name ?? "Chat", state);
}

function showAgentDockview(agentId: string): void {
  for (const [id, state] of agentDockviews) {
    state.container.style.display = id === agentId ? "block" : "none";
    if (id === agentId) {
      requestAnimationFrame(() => {
        const rect = state.container.getBoundingClientRect();
        state.component.layout(rect.width, rect.height);
      });
    }
  }
}

async function executeDestroy(
  agentId: string,
  panelId: string,
  dockviewAgentId: string,
): Promise<void> {
  const chatChildren = getChatAgentsForParent(agentId);

  // Cascade: destroy children first
  for (const child of chatChildren) {
    try {
      await fetch(apiUrl(`/api/agents/${encodeURIComponent(child.id)}/destroy`), {
        method: "POST",
      });
    } catch {
      // Continue even if a child destroy fails
    }
  }

  // Destroy the target agent
  try {
    const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/destroy`), {
      method: "POST",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = (data as { detail?: string }).detail ?? "Unknown error";
      alert(`Failed to destroy agent: ${detail}`);
      return;
    }
  } catch (e) {
    alert(`Failed to destroy agent: ${(e as Error).message}`);
    return;
  }

  // Remove the panel from dockview
  const state = agentDockviews.get(dockviewAgentId);
  if (state) {
    const panel = state.component.panels.find((p) => p.id === panelId);
    if (panel) {
      state.component.removePanel(panel);
    }

    // Also remove any child chat panels
    for (const child of chatChildren) {
      const childPanelId = `chat-${child.id}`;
      const childPanel = state.component.panels.find((p) => p.id === childPanelId);
      if (childPanel) {
        state.component.removePanel(childPanel);
      }
    }
  }

  // If the destroyed agent was a sidebar agent, auto-select another
  const isSidebarAgent = agentId === dockviewAgentId;
  if (isSidebarAgent) {
    // Clean up the dockview for this agent
    agentDockviews.delete(agentId);
    state?.container.remove();

    // Auto-select the first remaining sidebar agent
    const remaining = getSidebarAgents().filter((a) => a.id !== agentId);
    if (remaining.length > 0) {
      selectAgent(remaining[0].id);
    } else {
      selectAgent("");
    }
  }

  m.redraw();
}

export const DockviewWorkspace: m.Component<{ agentId: string | null }> = {
  oncreate(vnode: m.VnodeDOM<{ agentId: string | null }>) {
    wrapperElement = vnode.dom as HTMLElement;
    const agentId = vnode.attrs.agentId;
    if (agentId) {
      if (!agentDockviews.has(agentId)) {
        initializeAgentDockview(agentId, wrapperElement);
      } else {
        showAgentDockview(agentId);
      }
      currentAgentId = agentId;
    }
  },

  onupdate(vnode: m.VnodeDOM<{ agentId: string | null }>) {
    const agentId = vnode.attrs.agentId;
    if (agentId === currentAgentId) {
      return;
    }
    currentAgentId = agentId;

    if (!agentId) {
      for (const state of agentDockviews.values()) {
        state.container.style.display = "none";
      }
      return;
    }

    if (!wrapperElement) return;

    if (!agentDockviews.has(agentId)) {
      initializeAgentDockview(agentId, wrapperElement);
    } else {
      showAgentDockview(agentId);
    }
  },

  view(vnode) {
    const agentId = vnode.attrs.agentId;

    if (!agentId) {
      return m(
        "div",
        { class: "dockview-workspace flex items-center justify-center h-full" },
        m("p", { class: "text-text-secondary" }, "Select an agent to view its conversation."),
      );
    }

    return m("div", {
      class: "dockview-workspace",
      style: "width: 100%; height: 100%;",
    }, [
      showNewChatModal && newChatParentAgentId
        ? m(CreateAgentModal, {
            mode: "chat",
            parentAgentId: newChatParentAgentId,
            onCreated(newAgentId: string, newAgentName: string) {
              showNewChatModal = false;
              const state = agentDockviews.get(agentId);
              if (state) {
                focusOrCreateChatPanelForAgent(agentId, newAgentId, newAgentName, state);
              }
            },
            onCancel() {
              showNewChatModal = false;
            },
          })
        : null,

      showDestroyDialog && destroyTargetAgentId && destroyTargetAgentName
        ? m(DestroyConfirmDialog, {
            agentName: destroyTargetAgentName,
            chatChildren: getChatAgentsForParent(destroyTargetAgentId),
            onConfirm() {
              showDestroyDialog = false;
              const targetId = destroyTargetAgentId!;
              const panelId = destroyTargetPanelId!;
              const dvAgentId = destroyTargetDockviewAgentId!;
              destroyTargetAgentId = null;
              destroyTargetAgentName = null;
              destroyTargetPanelId = null;
              destroyTargetDockviewAgentId = null;
              executeDestroy(targetId, panelId, dvAgentId);
            },
            onCancel() {
              showDestroyDialog = false;
              destroyTargetAgentId = null;
              destroyTargetAgentName = null;
              destroyTargetPanelId = null;
              destroyTargetDockviewAgentId = null;
            },
          })
        : null,

      showShareModal && shareServerName
        ? m(ShareModal, {
            serverName: shareServerName,
            onClose() {
              showShareModal = false;
              shareServerName = null;
            },
          })
        : null,
    ]);
  },
};
