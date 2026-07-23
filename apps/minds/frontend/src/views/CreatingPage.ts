// Creating-page flow: the workspace is created in the background, so this
// page shows a loading screen (progress bar + rotating hints) and redirects
// into the workspace once creation finishes. The generic v1 operations
// resource is the source of truth for completion: the SSE 'done' event can be
// missed on a page reload (the log queue may already be drained), so
// completion is polled from the operation status; SSE only fills the live log
// view.
import m from "mithril";

import type { CreatingBootIsland } from "../chrome_state";
import type { EventSourceLike } from "../host";
import { getHost } from "../host";
import { MindsUIError, mountWithTeardown, readBootState, requireElement } from "../mount";
import { buttonClasses } from "../ui";

const STATUS_POLL_INTERVAL_MS = 2000;
// 8s per tip: long enough to comfortably read a full sentence before it
// swaps (workspace setup takes minutes, so there is no rush).
const TIP_ROTATION_MS = 8000;
const TIP_FADE_MS = 250;

// Static vnode builders (not shared vnode instances: mithril vnodes are
// single-use, so each rotation builds a fresh tree).
const TIPS: Array<() => m.Children> = [
  () => "Tip: your workspace is backed up automatically so your work survives a restart.",
  () => ["Did you know: in ", m("b", "privacy mode"), ", the data we gather stays on your own computer."],
  () => "Tip: switch accounts anytime from the workspace menu.",
  () => "Tip: share a running app with a teammate from the workspace’s Share menu.",
  () => "Did you know: you can revisit permissions and compute settings later.",
];

function operationUrl(creationId: string): string {
  return `/api/v1/workspaces/operations/create/${encodeURIComponent(creationId)}`;
}

interface CreateOperationStatus {
  status?: string;
  redirect_url?: string;
  error?: string;
  error_kind?: string;
  status_text?: string;
}

class CreatingController {
  isFailed = false;
  error = "";
  errorKind = "";
  stageText: string;
  tipIndex = 0;
  isTipVisible = true;
  areLogsShown = false;
  logText = "";

  readonly expectedDurationSeconds: number;
  private readonly creationId: string;
  private readonly startedAtMs: number;
  private isDone = false;
  private statusPollTimer: ReturnType<typeof setInterval> | null = null;
  private tipTimer: ReturnType<typeof setInterval> | null = null;
  private tipFadeTimer: ReturnType<typeof setTimeout> | null = null;
  private eventSource: EventSourceLike | null = null;
  private pendingLogLines: string[] = [];
  private isLogFlushScheduled = false;
  private isProgressLoopStopped = false;
  // The progress fill element, registered by the view's oncreate. The bar
  // animates at rAF cadence via direct style writes -- a 60fps m.redraw of
  // the whole page for a width tween would be waste.
  private barFillElement: HTMLElement | null = null;

  constructor(creationId: string, initialStageText: string, expectedDurationSeconds: number) {
    this.creationId = creationId;
    this.stageText = initialStageText;
    this.expectedDurationSeconds = expectedDurationSeconds > 0 ? expectedDurationSeconds : 60;
    this.startedAtMs = performance.now();
  }

  start(): void {
    this.tipTimer = setInterval(() => this.rotateTip(), TIP_ROTATION_MS);
    requestAnimationFrame(() => this.tickProgress());
    this.openLogStream();
    void this.pollStatus();
    this.statusPollTimer = setInterval(() => void this.pollStatus(), STATUS_POLL_INTERVAL_MS);
  }

  stop(): void {
    this.isProgressLoopStopped = true;
    if (this.statusPollTimer !== null) {
      clearInterval(this.statusPollTimer);
      this.statusPollTimer = null;
    }
    if (this.tipTimer !== null) {
      clearInterval(this.tipTimer);
      this.tipTimer = null;
    }
    if (this.tipFadeTimer !== null) {
      clearTimeout(this.tipFadeTimer);
      this.tipFadeTimer = null;
    }
    if (this.eventSource !== null) {
      this.eventSource.close();
      this.eventSource = null;
    }
  }

  registerBarFill(element: HTMLElement): void {
    this.barFillElement = element;
  }

  toggleLogs(): void {
    this.areLogsShown = !this.areLogsShown;
    m.redraw();
  }

  applyStatus(data: CreateOperationStatus | null): void {
    if (data === null) return;
    if (data.status === "DONE" && typeof data.redirect_url === "string" && data.redirect_url !== "") {
      this.isDone = true;
      if (this.statusPollTimer !== null) {
        clearInterval(this.statusPollTimer);
        this.statusPollTimer = null;
      }
      if (this.barFillElement !== null) this.barFillElement.style.width = "100%";
      // The workspace is ready: hand the /goto/<agent>/ URL to the host. In
      // Electron that routes it onto the caged content view instead of
      // navigating this (chrome) surface into untrusted agent content; plain
      // browser full-page navigates.
      getHost().navigate(data.redirect_url);
    } else if (data.status === "FAILED") {
      this.isFailed = true;
      this.error = data.error !== undefined && data.error !== "" ? data.error : "unknown error";
      this.errorKind = data.error_kind ?? "";
      // The prominent error box now carries the message; clear the faint
      // footer caption to avoid showing it twice.
      this.stageText = "";
      if (this.statusPollTimer !== null) {
        clearInterval(this.statusPollTimer);
        this.statusPollTimer = null;
      }
      if (this.tipTimer !== null) {
        clearInterval(this.tipTimer);
        this.tipTimer = null;
      }
      m.redraw();
    } else if (typeof data.status_text === "string" && data.status_text !== "" && !this.isFailed) {
      // Live stage caption (e.g. "Cloning repository...") from the create
      // operation status.
      if (data.status_text !== this.stageText) {
        this.stageText = data.status_text;
        m.redraw();
      }
    }
  }

  private rotateTip(): void {
    this.isTipVisible = false;
    m.redraw();
    this.tipFadeTimer = setTimeout(() => {
      this.tipIndex = (this.tipIndex + 1) % TIPS.length;
      this.isTipVisible = true;
      m.redraw();
    }, TIP_FADE_MS);
  }

  // Time-based bar: ease to 80% over the expected duration, then crawl the
  // last 20% asymptotically. Snaps to 100% when creation actually completes.
  private progressForElapsed(elapsedSeconds: number): number {
    const expected = this.expectedDurationSeconds;
    if (elapsedSeconds <= expected) return 80 * (elapsedSeconds / expected);
    return 80 + 20 * (1 - Math.exp(-(elapsedSeconds - expected) / expected));
  }

  private tickProgress(): void {
    if (this.isProgressLoopStopped || this.isFailed || this.isDone) return;
    const elapsedSeconds = (performance.now() - this.startedAtMs) / 1000;
    const pct = Math.min(99.5, this.progressForElapsed(elapsedSeconds));
    if (this.barFillElement !== null) this.barFillElement.style.width = `${pct.toFixed(1)}%`;
    requestAnimationFrame(() => this.tickProgress());
  }

  private async pollStatus(): Promise<void> {
    try {
      const response = await fetch(operationUrl(this.creationId));
      if (!response.ok) return;
      this.applyStatus((await response.json()) as CreateOperationStatus);
    } catch {
      // Transient poll failure; the next tick retries.
    }
  }

  // SSE: live logs only. Frames are batched per animation frame so a burst
  // of build output costs one redraw, not one per line.
  private openLogStream(): void {
    const source = new EventSource(`${operationUrl(this.creationId)}/logs`) as EventSourceLike;
    this.eventSource = source;
    source.addEventListener("message", (event) => {
      let data: { log?: string; done?: boolean };
      try {
        data = JSON.parse(event.data) as { log?: string; done?: boolean };
      } catch {
        return;
      }
      if (data.done === true) {
        source.close();
        this.eventSource = null;
        this.flushLogs();
      } else if (typeof data.log === "string" && data.log !== "") {
        this.pendingLogLines.push(data.log);
        if (!this.isLogFlushScheduled) {
          this.isLogFlushScheduled = true;
          requestAnimationFrame(() => this.flushLogs());
        }
      }
    });
    source.addEventListener("error", () => {
      if (this.eventSource !== null) {
        this.eventSource.close();
        this.eventSource = null;
      }
    });
  }

  private flushLogs(): void {
    this.isLogFlushScheduled = false;
    if (this.pendingLogLines.length === 0) return;
    this.logText += this.pendingLogLines.join("\n") + "\n";
    this.pendingLogLines = [];
    m.redraw();
  }
}

interface CreatingPageAttrs {
  controller: CreatingController;
}

// Private-repo guidance, revealed only when the backend classifies the
// failure as GITHUB_AUTH_REQUIRED (the workspace source is a github.com URL
// none of this computer's git credentials can see). Static copy gated on the
// error kind; the backend only classifies, it never generates prose.
function githubAuthHelp(): m.Children {
  return m("div", { id: "github-auth-help", class: "text-left type-body text-secondary mt-3" }, [
    m("p", [
      "This repository looks private, or it does not exist: GitHub asked for credentials, and none on this ",
      "computer could open it. If you have access to the repository, sign in to GitHub on this computer and ",
      "try again.",
    ]),
    m("p", { class: "mt-2" }, [
      "The simplest way to sign in is the GitHub CLI: run ",
      m("code", { class: "font-mono" }, "gh auth login"),
      " in a terminal and follow the prompts (see the ",
      m(
        "a",
        {
          href: "https://docs.github.com/en/github-cli/github-cli/quickstart",
          target: "_blank",
          rel: "noopener",
          class: "text-accent hover:underline",
        },
        "GitHub CLI quickstart",
      ),
      "). Git then uses those credentials automatically when cloning.",
    ]),
    m("p", { class: "mt-2" }, [
      "Alternatively, clone or download the repository yourself to any folder on this computer, and enter ",
      "that folder's path in the form instead of the URL.",
    ]),
  ]);
}

// Generic (non-GitHub) git remote: same guidance but without the GitHub-CLI
// advice, which only fits github.com. Revealed on GIT_AUTH_REQUIRED.
function gitAuthHelp(): m.Children {
  return m("div", { id: "git-auth-help", class: "text-left type-body text-secondary mt-3" }, [
    m("p", [
      "This repository looks private, or it does not exist: it asked for credentials, and none on this ",
      "computer could open it. If you have access to it, make sure your git credentials for this host are ",
      "set up on this computer, then try again.",
    ]),
    m("p", { class: "mt-2" }, [
      "Alternatively, clone or download the repository yourself to any folder on this computer, and enter ",
      "that folder's path in the form instead of the URL.",
    ]),
  ]);
}

function progressView(controller: CreatingController): m.Children {
  return m("div", { id: "progress-view" }, [
    m("div", { class: "text-center pt-2 pb-1" }, [
      m("div", { class: "type-heading mb-1.5" }, "Setting up your workspace"),
      m(
        "p",
        {
          id: "tip",
          class: "type-body text-secondary min-h-[34px] transition-opacity",
          style: { opacity: controller.isTipVisible ? "1" : "0" },
        },
        TIPS[controller.tipIndex](),
      ),
    ]),
    m("div", { class: "h-1.5 bg-fill-subtle rounded-full overflow-hidden" }, [
      m("div", {
        id: "bar-fill",
        class: "h-full w-0 rounded-full bg-accent transition-[width] duration-300 ease-out",
        oncreate: (barVnode) => controller.registerBarFill(barVnode.dom as HTMLElement),
      }),
    ]),
  ]);
}

// Failure sub-view, rendered the moment creation fails. Keeps the element
// ids the e2e workspace runner asserts on (#failure-view, #error-message).
function failureView(controller: CreatingController): m.Children {
  return m("div", { id: "failure-view" }, [
    m("div", { class: "text-center pt-2 pb-1" }, [
      m("div", { class: "type-heading mb-1.5 text-important" }, "We couldn't set up your workspace"),
      m("p", { class: "type-body text-secondary" }, "Setup stopped before your workspace was ready."),
    ]),
    m(
      "div",
      { class: "px-3 py-2 rounded-md type-body my-2 bg-[var(--c-important-surface)] text-important" },
      m(
        "span",
        { id: "error-message", class: "font-mono type-helper break-words whitespace-pre-wrap" },
        controller.error,
      ),
    ),
    controller.errorKind === "GITHUB_AUTH_REQUIRED" ? githubAuthHelp() : null,
    controller.errorKind === "GIT_AUTH_REQUIRED" ? gitAuthHelp() : null,
    m("div", { class: "flex items-center justify-center gap-2 mt-4" }, [
      m("a", { href: "/create", class: buttonClasses("primary") }, "Back to setup"),
      m("a", { href: "/", class: buttonClasses("ghost") }, "Home"),
    ]),
  ]);
}

function CreatingPage(): m.Component<CreatingPageAttrs> {
  let lastLogLength = 0;

  return {
    view(vnode) {
      const { controller } = vnode.attrs;
      return m("div", { class: "w-full" }, [
        controller.isFailed ? failureView(controller) : progressView(controller),
        // Shared footer: live stage caption + collapsible logs, in one place
        // regardless of which sub-view is showing.
        m("div", { class: "flex items-center justify-between mt-2 type-helper text-tertiary" }, [
          m("span", { id: "stage" }, controller.stageText),
          m(
            "button",
            {
              type: "button",
              id: "details-toggle",
              class: "inline-flex items-center gap-1 text-tertiary hover:text-primary cursor-pointer",
              onclick: () => controller.toggleLogs(),
            },
            controller.areLogsShown ? "Hide details" : "Show details",
          ),
        ]),
        controller.areLogsShown
          ? m(
              "div",
              {
                id: "logs",
                class:
                  "dark mt-3 p-3 bg-surface-primary text-secondary font-mono type-helper rounded-lg " +
                  "max-h-[260px] overflow-y-auto whitespace-pre-wrap border border-subtle",
                oncreate: (logsVnode) => {
                  lastLogLength = controller.logText.length;
                  logsVnode.dom.scrollTop = logsVnode.dom.scrollHeight;
                },
                onupdate: (logsVnode) => {
                  if (controller.logText.length !== lastLogLength) {
                    lastLogLength = controller.logText.length;
                    logsVnode.dom.scrollTop = logsVnode.dom.scrollHeight;
                  }
                },
              },
              controller.logText,
            )
          : null,
      ]);
    },
  };
}

export function mountCreating(target: Element | null): void {
  const el = requireElement(target, "creating page container");
  const island = readBootState() as CreatingBootIsland;
  if (island.creating === undefined) {
    throw new MindsUIError("creating boot island is missing the creating slice");
  }
  const extras = island.creating;
  const controller = new CreatingController(extras.agent_id, extras.status_text, extras.expected_duration_seconds);
  controller.start();
  mountWithTeardown(el, {
    view: () => m(CreatingPage, { controller }),
    // Fires when the swap engine (or a test) unmounts the page; timers, the
    // rAF loop, and the SSE stream must not outlive the component.
    onremove: () => controller.stop(),
  });
}
