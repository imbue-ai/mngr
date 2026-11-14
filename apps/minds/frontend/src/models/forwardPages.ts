// What Imbue Studio draws over a workspace frame showing one of the workspace
// connection's (the forward proxy's) own pages.
//
// The proxy's pages say nothing about why they are there: a framed one posts
// its reason to the window framing it, and says when it goes away. This module
// reads those messages and decides what the frame's own notice says, which
// buttons fit, and whether the page is still re-attempting on its own (shown as
// a spinner, never as a sentence about retrying).

import type { UserFacingError } from "./errors";
import { isWorkspaceOrigin } from "./workspaces";

/** The proxy's reasons (`X-Mngr-Forward-Reason`, mngr_forward's ProxyAnswerReason). */
const FORWARD_PAGE_REASONS = [
  "DISCOVERY_PENDING",
  "UNKNOWN_AGENT",
  "NO_ROUTE",
  "NO_ROUTE_EVENTS_STOPPED",
  "LOCAL_CONNECTION_FAILED",
  "UNREACHABLE",
  "NOT_LISTENING",
  "NOT_ANSWERING",
  "CONNECTION_LOST",
  "TIMED_OUT",
  "POOL_EXHAUSTED",
] as const;
export type ForwardPageReason = (typeof FORWARD_PAGE_REASONS)[number];

function isOneOf<T extends string>(values: readonly T[], value: unknown): value is T {
  return (values as readonly unknown[]).includes(value);
}

/** One of the proxy's own pages, as it reports itself. */
export interface ForwardPageReport {
  reason: ForwardPageReason;
  /** The page's HTTP status: 503 while it re-attempts, 502/504 once the
   * connection dropped. */
  status: number;
  /** The proxy's plain-text answer. */
  details: string;
}

/** A message from a frame: its page reporting itself, or going away. */
export type ForwardPageMessage =
  | { kind: "report"; report: ForwardPageReport }
  | { kind: "closed" };

/** Read a frame's message, or null for any other message (including a page
 * reporting a reason this build does not know, whose own page then stands). */
export function parseForwardPageMessage(data: unknown): ForwardPageMessage | null {
  if (data === null || typeof data !== "object") return null;
  const record = data as Record<string, unknown>;
  if (record.type === "mngr-forward:page-closed") return { kind: "closed" };
  if (record.type !== "mngr-forward:page") return null;
  if (!isOneOf(FORWARD_PAGE_REASONS, record.reason) || typeof record.status !== "number") return null;
  return {
    kind: "report",
    report: {
      reason: record.reason,
      status: record.status,
      details: typeof record.details === "string" ? record.details : "",
    },
  };
}

/** How long a page that is re-attempting shows only its spinner before it says
 * why, since most waits end before then. */
export const FORWARD_PAGE_SPINNER_ONLY_MS = 10_000;

/** A message as the window framing a page received it. */
export interface FramedMessage {
  data: unknown;
  origin: string;
  /** Posted from the frame's own window. */
  isFromFrame: boolean;
}

/** The proxy page a frame holds, followed from the messages the page posts. */
export class ForwardPageTracker {
  private held: { report: ForwardPageReport; origin: string; sinceMs: number } | null = null;

  constructor(private readonly now: () => number = Date.now) {}

  get report(): ForwardPageReport | null {
    return this.held?.report ?? null;
  }

  /** How long the page has left of showing only its spinner; 0 once it is over. */
  msUntilExplained(): number {
    if (this.held === null) return 0;
    return Math.max(0, this.held.sinceMs + FORWARD_PAGE_SPINNER_ONLY_MS - this.now());
  }

  /** Take one message; whether the page the frame holds changed. */
  receive(message: FramedMessage): boolean {
    if (!isWorkspaceOrigin(message.origin)) return false;
    const parsed = parseForwardPageMessage(message.data);
    if (parsed === null) return false;
    if (parsed.kind === "closed") {
      // Posted from a document that is unloading, so it arrives with no
      // source; the origin of the page that reported is what ties it.
      if (this.held === null || this.held.origin !== message.origin) return false;
      this.held = null;
      return true;
    }
    if (!message.isFromFrame) return false;
    this.held = { report: parsed.report, origin: message.origin, sinceMs: this.held?.sinceMs ?? this.now() };
    return true;
  }

  /** Forget the page, for a frame that navigated; whether one was held. */
  clear(): boolean {
    const wasHeld = this.held !== null;
    this.held = null;
    return wasHeld;
  }
}

export type ForwardPageActionKind = "try-again" | "restart-app" | "back-to-workspaces";

export interface ForwardPageAction {
  kind: ForwardPageActionKind;
  label: string;
}

/** The frame's notice for one page. */
export interface ForwardPageNotice {
  /** The page re-attempts on its own: the notice shows a spinner. */
  isWaiting: boolean;
  /** The failure is settled as soon as it is reported, so it shows without the
   * spinner-only seconds a wait that may still resolve gets. */
  isExplainedAtOnce: boolean;
  /** Above the spinner; null when the notice is only its error. */
  heading: string | null;
  /** A plain sentence for a wait, with no error to report. */
  sentence: string | null;
  /** A failure the user can act on, drawn with the shared error display. */
  error: UserFacingError | null;
  actions: ForwardPageAction[];
  isBugReportOffered: boolean;
}

export interface ForwardPageContext {
  /** The desktop app, which can restart itself. */
  isRestartAppAvailable: boolean;
  /** The workspace the frame is for is in this window's workspaces list. */
  isWorkspaceListed: boolean;
}

const TRY_AGAIN: ForwardPageAction = { kind: "try-again", label: "Try again" };
const RESTART_APP: ForwardPageAction = {
  kind: "restart-app",
  label: "Restart Imbue Studio",
};
const BACK_TO_WORKSPACES: ForwardPageAction = {
  kind: "back-to-workspaces",
  label: "Back to workspaces",
};

/** The sentence for each wait the page re-attempts on its own. */
const WAITING_SENTENCES: Partial<Record<ForwardPageReason, string>> = {
  NO_ROUTE: "Imbue Studio can't open this workspace right now.",
  NO_ROUTE_EVENTS_STOPPED: "Imbue Studio can't open this workspace right now.",
  UNREACHABLE: "Imbue Studio can't reach this workspace right now.",
  NOT_LISTENING: "This workspace isn't answering right now.",
  NOT_ANSWERING: "This workspace isn't answering right now.",
};

/** The proxy's own account of the page, for See details and a bug report. */
function proxyDetails(report: ForwardPageReport): string {
  return `HTTP ${report.status} ${report.reason}: ${report.details}`;
}

const LOADING_HEADING = "Loading workspace";

function notice(fields: Partial<ForwardPageNotice>): ForwardPageNotice {
  return {
    isWaiting: false,
    isExplainedAtOnce: false,
    heading: null,
    sentence: null,
    error: null,
    actions: [],
    isBugReportOffered: true,
    ...fields,
  };
}

/** Whether `notice` still shows only its spinner, `msUntilExplained` before
 * its page's spinner-only seconds end. */
export function isNoticeSpinnerOnly(notice: ForwardPageNotice, msUntilExplained: number): boolean {
  return notice.isWaiting && !notice.isExplainedAtOnce && msUntilExplained > 0;
}

/** What the frame shows for `report`. */
export function forwardPageNotice(report: ForwardPageReport, context: ForwardPageContext): ForwardPageNotice {
  const heading = LOADING_HEADING;
  if (report.status !== 503) {
    return notice({
      error: {
        message: context.isRestartAppAvailable
          ? "The connection to this workspace dropped."
          : "The connection to this workspace dropped. If this keeps happening, restart the Imbue Studio app on the computer running it.",
        details: proxyDetails(report),
      },
      actions: context.isRestartAppAvailable
        ? [RESTART_APP, TRY_AGAIN]
        : [TRY_AGAIN],
    });
  }
  switch (report.reason) {
    case "DISCOVERY_PENDING":
      return notice({ isWaiting: true, heading });
    case "UNKNOWN_AGENT":
      // Discovery only has to have reported once, from any provider, so a
      // workspace on a provider that has not reported yet reads this way for a
      // moment, as does any workspace before the list has loaded: both get the
      // spinner-only seconds. Past them nothing says the wait will end, so the
      // user gets a way out while the page keeps re-attempting.
      return context.isWorkspaceListed
        ? notice({
            isWaiting: true,
            heading,
            error: { message: "Imbue Studio can't find this workspace right now.", details: proxyDetails(report) },
            actions: [BACK_TO_WORKSPACES],
          })
        : notice({
            isWaiting: true,
            heading,
            error: {
              message: "This workspace isn't in your workspaces list. It may have been removed.",
              details: "",
            },
            actions: [BACK_TO_WORKSPACES],
            isBugReportOffered: false,
          });
    case "LOCAL_CONNECTION_FAILED":
      // Raised by this device's own end of the connection after the far side
      // answered; time does not mend it, a restart does.
      return notice({
        isWaiting: true,
        isExplainedAtOnce: true,
        heading,
        error: {
          message: context.isRestartAppAvailable
            ? "Imbue Studio can't connect to this workspace from this device. Restarting Imbue Studio usually fixes this."
            : "Imbue Studio can't connect to this workspace from this device. Restarting the Imbue Studio app on the computer running it usually fixes this.",
          details: proxyDetails(report),
        },
        actions: context.isRestartAppAvailable ? [RESTART_APP] : [],
      });
  }
  return notice({ isWaiting: true, heading, sentence: WAITING_SENTENCES[report.reason] ?? null });
}
