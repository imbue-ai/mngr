/**
 * Proto-agent log viewer. Opens a WebSocket to stream creation logs
 * from a proto-agent (agent being created via mngr create).
 * On completion, redraws so the parent component can switch to the chat view.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

interface ProtoAgentLogViewAttrs {
  agentId: string;
}

export function ProtoAgentLogView(): m.Component<ProtoAgentLogViewAttrs> {
  let ws: WebSocket | null = null;
  let lines: string[] = [];
  let done = false;
  let success = false;
  let error: string | null = null;
  let currentAgentId: string | null = null;

  function connectWs(agentId: string): void {
    if (ws !== null) {
      ws.close();
    }
    lines = [];
    done = false;
    success = false;
    error = null;
    currentAgentId = agentId;

    const base = apiUrl(`/api/proto-agents/${encodeURIComponent(agentId)}/logs`);
    const loc = window.location;
    const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
    let url: string;
    if (base.startsWith("http")) {
      url = base.replace(/^http/, "ws");
    } else {
      url = `${protocol}//${loc.host}${base}`;
    }

    ws = new WebSocket(url);

    ws.onmessage = (event: MessageEvent) => {
      const data = JSON.parse(event.data as string) as
        | { line: string }
        | { done: true; success: boolean; error: string | null };

      if ("line" in data) {
        lines.push(data.line);
      } else if ("done" in data) {
        done = true;
        success = data.success;
        error = data.error;
      }
      m.redraw();
    };

    ws.onclose = () => {
      ws = null;
    };

    ws.onerror = () => {
      ws?.close();
    };
  }

  function disconnect(): void {
    if (ws !== null) {
      ws.close();
      ws = null;
    }
    currentAgentId = null;
  }

  return {
    oncreate(vnode) {
      connectWs(vnode.attrs.agentId);
    },

    onupdate(vnode) {
      if (vnode.attrs.agentId !== currentAgentId) {
        connectWs(vnode.attrs.agentId);
      }
      const container = vnode.dom.querySelector(".proto-agent-log-lines");
      if (container) {
        container.scrollTop = container.scrollHeight;
      }
    },

    onremove() {
      disconnect();
    },

    view(vnode) {
      const agentId = vnode.attrs.agentId;

      return m(
        "div.proto-agent-log-view",
        { style: "display: flex; flex-direction: column; height: 100%; padding: 16px;" },
        [
          m(
            "div",
            { style: "font-weight: 600; margin-bottom: 8px; font-size: 0.9em; color: #666;" },
            done
              ? success
                ? `Agent ${agentId} created successfully`
                : `Agent creation failed`
              : `Creating agent ${agentId}...`,
          ),
          error ? m("div", { style: "color: red; margin-bottom: 8px; font-size: 0.85em;" }, error) : null,
          m(
            "div.proto-agent-log-lines",
            {
              style:
                "flex: 1; overflow-y: auto; background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 0.8em; padding: 12px; border-radius: 4px; white-space: pre-wrap; word-break: break-all;",
            },
            lines.map((line, i) => m("div", { key: i, style: "line-height: 1.5;" }, line)),
          ),
        ],
      );
    },
  };
}
