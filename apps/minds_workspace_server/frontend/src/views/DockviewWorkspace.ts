/**
 * Dockview-based tabbed workspace for the main content area.
 * Manages one DockviewComponent per agent, hiding/showing as agents are selected.
 */

import m from "mithril";
import {
  DockviewComponent,
  type IContentRenderer,
  type GroupPanelPartInitParameters,
  type IHeaderActionsRenderer,
  type SerializedDockview,
} from "dockview-core";
import "dockview-core/dist/styles/dockview.css";
import { ChatPanel } from "./ChatPanel";
import { IframePanel } from "./IframePanel";
import { SubagentView } from "./SubagentView";
import { apiUrl } from "../base-path";
import { getAgents, getAgentsLoaded } from "../models/Conversation";

const TERMINAL_URL = "http://localhost:7681";
const AUTOSAVE_DEBOUNCE_MS = 1500;

type PanelType = "chat" | "terminal" | "iframe" | "subagent";

interface PanelParams {
  panelType: PanelType;
  agentId: string;
  url?: string;
  title?: string;
  subagentSessionId?: string;
}

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

function getAgentName(agentId: string): string {
  const agents = getAgents();
  const agent = agents.find((a) => a.id === agentId);
  if (agent?.name) return agent.name;
  return getAgentsLoaded() ? "Unknown agent" : "Loading...";
}

function createMithrilRenderer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: m.ComponentTypes<any, any>,
  attrs: Record<string, unknown>,
): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  element.style.overflow = "hidden";

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

  const items: Array<{ label: string; action: () => void }> = [
    {
      label: "Chat",
      action: () => {
        focusOrCreateChatPanel(agentId, dockviewState);
      },
    },
    {
      label: "Terminal",
      action: () => {
        openIframeTab(agentId, dockviewState, TERMINAL_URL, "Terminal", "terminal");
      },
    },
    {
      label: "Custom URL",
      action: () => {
        showCustomUrlDialog(agentId, dockviewState);
      },
    },
  ];

  for (const item of items) {
    const menuItem = document.createElement("div");
    menuItem.className = "dockview-add-tab-dropdown-item";
    menuItem.textContent = item.label;
    menuItem.addEventListener("click", (e) => {
      e.stopPropagation();
      dropdown.style.display = "none";
      item.action();
    });
    dropdown.appendChild(menuItem);
  }

  element.appendChild(dropdown);

  button.addEventListener("click", (e) => {
    e.stopPropagation();
    const isVisible = dropdown.style.display !== "none";
    dropdown.style.display = isVisible ? "none" : "block";
  });

  // Close dropdown when clicking outside
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

function focusOrCreateChatPanel(agentId: string, state: AgentDockviewState): void {
  // Chat is singleton -- focus if it exists
  const existingPanel = state.component.panels.find((p) => {
    const params = state.panelParams.get(p.id);
    return params?.panelType === "chat";
  });
  if (existingPanel) {
    state.component.setActivePanel(existingPanel);
    return;
  }
  // Should not normally happen since chat is created on init and unclosable
  addChatPanel(agentId, state);
}

function addChatPanel(agentId: string, state: AgentDockviewState): void {
  const panelId = `chat-${agentId}`;
  const params: PanelParams = { panelType: "chat", agentId };
  state.panelParams.set(panelId, params);
  state.component.addPanel({
    id: panelId,
    component: "chat",
    title: getAgentName(agentId),
    params,
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

export function openSubagentTab(agentId: string, subagentSessionId: string, description: string): void {
  const state = agentDockviews.get(agentId);
  if (!state) return;

  // Check if this subagent tab is already open
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

  try {
    await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/layout`), {
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
  try {
    const response = await fetch(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/layout`));
    if (!response.ok) return null;
    return (await response.json()) as SavedLayout;
  } catch {
    return null;
  }
}

function createDockviewForAgent(agentId: string, parentElement: HTMLElement): AgentDockviewState {
  const container = document.createElement("div");
  container.className = "dockview-agent-container dockview-theme-dark";
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
    createComponent(options) {
      const params = (options as unknown as { params?: PanelParams }).params ?? panelParams.get(options.id);

      switch (options.name) {
        case "chat":
          return createMithrilRenderer(ChatPanel, { agentId: params?.agentId ?? agentId });

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
    createRightHeaderActionComponent() {
      return createAddTabButton(agentId, state);
    },
    createTabComponent(options) {
      const params = panelParams.get(options.id);
      if (params?.panelType === "chat") {
        // Custom tab without close button for chat
        return createUnclosableTab();
      }
      return undefined; // Use default tab (with close button)
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

function createUnclosableTab(): {
  element: HTMLElement;
  init: (params: GroupPanelPartInitParameters) => void;
  dispose?: () => void;
} {
  const element = document.createElement("div");
  element.className = "dv-default-tab";

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  element.appendChild(content);

  // No action/close button

  let disposables: Array<{ dispose: () => void }> = [];

  return {
    element,
    init(params: GroupPanelPartInitParameters) {
      content.textContent = params.title ?? "";
      const sub = params.api.onDidTitleChange((event) => {
        content.textContent = event.title;
      });
      disposables.push(sub);
    },
    dispose() {
      for (const d of disposables) {
        d.dispose();
      }
      disposables = [];
    },
  };
}

async function initializeAgentDockview(agentId: string, parentElement: HTMLElement): Promise<void> {
  const state = createDockviewForAgent(agentId, parentElement);
  agentDockviews.set(agentId, state);

  const saved = await loadLayout(agentId);

  if (saved) {
    // Restore panel params before fromJSON so createComponent can access them
    for (const [id, params] of Object.entries(saved.panelParams)) {
      state.panelParams.set(id, params);
    }
    try {
      state.component.fromJSON(saved.dockview);
      // Update chat tab title in case agent name changed
      for (const panel of state.component.panels) {
        const params = state.panelParams.get(panel.id);
        if (params?.panelType === "chat") {
          panel.api.setTitle(getAgentName(agentId));
        }
      }
      return;
    } catch {
      // If restore fails, fall through to default layout
      state.panelParams.clear();
    }
  }

  // Default layout: single chat tab
  addChatPanel(agentId, state);
}

function showAgentDockview(agentId: string): void {
  // Hide all agent containers
  for (const [id, state] of agentDockviews) {
    state.container.style.display = id === agentId ? "block" : "none";
    if (id === agentId) {
      // Trigger layout recalculation after showing
      requestAnimationFrame(() => {
        const rect = state.container.getBoundingClientRect();
        state.component.layout(rect.width, rect.height);
      });
    }
  }
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
      // Update chat tab title if agent name might have changed
      if (agentId) {
        const state = agentDockviews.get(agentId);
        if (state) {
          for (const panel of state.component.panels) {
            const params = state.panelParams.get(panel.id);
            if (params?.panelType === "chat") {
              const name = getAgentName(agentId);
              if (panel.api.title !== name) {
                panel.api.setTitle(name);
              }
            }
          }
        }
      }
      return;
    }
    currentAgentId = agentId;

    if (!agentId) {
      // Hide all
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
    });
  },
};
