// Destroy detail page: drives the log tail + status badge from the
// type-segmented operation resource. Status is polled from
// /api/v1/workspaces/operations/destroy/<id> (authoritative completion
// signal) and the live log streams over SSE from
// .../operations/destroy/<id>/logs. Retry re-issues the v1 destroy; dismiss
// clears the operation record via DELETE on the operation resource.
import m from "mithril";

import type { DestroyingBootIsland } from "../chrome_state";
import type { EventSourceLike } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";
import { Spinner } from "./Spinner";
import { StatusBadge } from "./StatusBadge";

type DestroyStatus = "running" | "failed" | "done";

const REDIRECT_DELAY_MS = 800;
const STATUS_POLL_INTERVAL_MS = 1000;

function operationUrl(agentId: string): string {
  return `/api/v1/workspaces/operations/destroy/${encodeURIComponent(agentId)}`;
}

class DestroyingController {
  status: DestroyStatus;
  logText = "";
  isRetryInFlight = false;
  isDismissInFlight = false;

  private readonly agentId: string;
  // True once a terminal status (done/failed) has been applied; later poll /
  // SSE results must not restart the flow.
  private isStopped = false;
  private statusPollTimer: ReturnType<typeof setInterval> | null = null;
  private redirectTimer: ReturnType<typeof setTimeout> | null = null;
  private eventSource: EventSourceLike | null = null;

  constructor(agentId: string, initialStatus: string) {
    this.agentId = agentId;
    this.status = initialStatus === "failed" || initialStatus === "done" ? initialStatus : "running";
  }

  start(): void {
    this.isStopped = false;
    this.openLogStream();
    void this.pollStatus();
    this.statusPollTimer = setInterval(() => void this.pollStatus(), STATUS_POLL_INTERVAL_MS);
  }

  stop(): void {
    if (this.statusPollTimer !== null) {
      clearInterval(this.statusPollTimer);
      this.statusPollTimer = null;
    }
    if (this.redirectTimer !== null) {
      clearTimeout(this.redirectTimer);
      this.redirectTimer = null;
    }
    this.closeLogStream();
  }

  // Apply an authoritative status (lowercased to match the badge vocabulary;
  // the v1 operation resource reports RUNNING / DONE / FAILED). ``done``
  // redirects Home shortly after the badge flips; ``failed`` reveals the
  // Retry / Dismiss actions.
  applyStatus(rawStatus: string): void {
    if (rawStatus === "" || this.isStopped) return;
    const status = rawStatus.toLowerCase();
    if (status !== "running" && status !== "failed" && status !== "done") return;
    if (status !== this.status) {
      this.status = status;
      m.redraw();
    }
    if (status === "done") {
      this.isStopped = true;
      this.stop();
      this.redirectTimer = setTimeout(() => {
        window.location.href = "/";
      }, REDIRECT_DELAY_MS);
    } else if (status === "failed") {
      this.isStopped = true;
      this.stop();
      m.redraw();
    }
  }

  async retry(): Promise<void> {
    this.isRetryInFlight = true;
    m.redraw();
    try {
      const response = await fetch(`/api/v1/workspaces/${encodeURIComponent(this.agentId)}/destroy`, {
        method: "POST",
      });
      if (!response.ok) {
        window.alert("Could not start retry");
        return;
      }
      // Reset state and start the log tail + status poll again.
      this.logText = "";
      this.status = "running";
      this.stop();
      this.start();
    } catch {
      window.alert("Could not start retry");
    } finally {
      this.isRetryInFlight = false;
      m.redraw();
    }
  }

  async dismiss(): Promise<void> {
    this.isDismissInFlight = true;
    m.redraw();
    try {
      await fetch(operationUrl(this.agentId), { method: "DELETE" });
    } catch {
      // The record is cleaned up opportunistically; go Home regardless.
    } finally {
      window.location.href = "/";
    }
  }

  private async pollStatus(): Promise<void> {
    try {
      const response = await fetch(operationUrl(this.agentId));
      if (!response.ok) return;
      const data = (await response.json()) as { status?: string };
      if (typeof data.status === "string") this.applyStatus(data.status);
    } catch {
      // Transient poll failure; the next tick retries.
    }
  }

  // Live log tail. The SSE replays the log from the start on (re)connect and
  // emits a final {"done": true, "status": ...} frame, which is a secondary
  // completion signal alongside the status poll.
  private openLogStream(): void {
    this.closeLogStream();
    const source = new EventSource(`${operationUrl(this.agentId)}/logs`) as EventSourceLike;
    this.eventSource = source;
    source.addEventListener("message", (event) => {
      let data: { log?: string; done?: boolean; status?: string };
      try {
        data = JSON.parse(event.data) as { log?: string; done?: boolean; status?: string };
      } catch {
        return;
      }
      if (typeof data.log === "string" && data.log !== "") {
        this.logText += data.log;
        m.redraw();
      }
      if (data.done === true) {
        this.closeLogStream();
        if (typeof data.status === "string") this.applyStatus(data.status);
      }
    });
    source.addEventListener("error", () => this.closeLogStream());
  }

  private closeLogStream(): void {
    if (this.eventSource !== null) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }
}

interface DestroyingPageAttrs {
  controller: DestroyingController;
  agentId: string;
  agentName: string;
  pid: number;
}

function statusIndicator(status: DestroyStatus): m.Children {
  if (status === "running") {
    return [m(Spinner), m("span", { class: "text-primary" }, "Running...")];
  }
  if (status === "failed") {
    return m(StatusBadge, { variant: "error" }, "Failed");
  }
  return m(StatusBadge, { variant: "success" }, "Done. Redirecting...");
}

function DestroyingPage(): m.Component<DestroyingPageAttrs> {
  // Autoscroll bookkeeping: pin the log view to the bottom only when new
  // content arrived (a redraw without growth must not fight user scrolling).
  let lastLogLength = 0;

  function autoscrollLog(dom: Element): void {
    dom.scrollTop = dom.scrollHeight;
  }

  return {
    view(vnode) {
      const { controller, agentId, agentName, pid } = vnode.attrs;
      return m("div", [
        m("h1", { class: "type-heading text-primary" }, `Destroying ${agentName}`),
        m("p", { class: "type-helper text-tertiary mb-4" }, `${agentId} · pid ${pid}`),
        m("div", { class: "my-4 flex items-center gap-2", id: "destroying-status" }, statusIndicator(controller.status)),
        m("h2", { class: "type-label text-secondary mt-6 mb-2" }, "Log"),
        m(
          "div",
          {
            id: "destroying-log",
            class:
              "dark p-3 bg-surface-primary text-secondary font-mono type-helper rounded-lg " +
              "max-h-[420px] overflow-y-auto whitespace-pre-wrap border border-subtle",
            oncreate: (logVnode) => {
              lastLogLength = controller.logText.length;
              autoscrollLog(logVnode.dom);
            },
            onupdate: (logVnode) => {
              if (controller.logText.length !== lastLogLength) {
                lastLogLength = controller.logText.length;
                autoscrollLog(logVnode.dom);
              }
            },
          },
          controller.logText,
        ),
        controller.status === "failed"
          ? m("div", { id: "destroying-actions", class: "mt-6 flex gap-3" }, [
              m(
                "button",
                {
                  id: "destroying-retry-btn",
                  type: "button",
                  class: buttonClasses("primary"),
                  disabled: controller.isRetryInFlight,
                  onclick: () => void controller.retry(),
                },
                "Retry",
              ),
              m(
                "button",
                {
                  id: "destroying-dismiss-btn",
                  type: "button",
                  class: buttonClasses("secondary"),
                  disabled: controller.isDismissInFlight,
                  onclick: () => void controller.dismiss(),
                },
                "Dismiss",
              ),
            ])
          : null,
        m("div", { class: "mt-8" }, [
          m("a", { href: "/", class: "text-accent hover:underline type-helper" }, "← Back to workspaces"),
        ]),
      ]);
    },
  };
}

export function mountDestroying(target: Element | null): void {
  const el = requireElement(target, "destroying page container");
  const island = readBootState() as DestroyingBootIsland;
  if (island.destroying === undefined) {
    throw new MindsUIError("destroying boot island is missing the destroying slice");
  }
  const extras = island.destroying;
  const controller = new DestroyingController(extras.agent_id, extras.status);
  controller.start();
  mountWithTeardown(el, {
    view: () =>
      m(DestroyingPage, {
        controller,
        agentId: extras.agent_id,
        agentName: extras.agent_name,
        pid: extras.pid,
      }),
    // Fires when the swap engine (or a test) unmounts the page; the
    // controller's timers and SSE stream must not outlive the component.
    onremove: () => controller.stop(),
  });
}
