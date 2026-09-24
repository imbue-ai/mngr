// Help model: bug-report + agent-assist flows against the legacy /help routes.
//
// Port of Help.jinja's script: two modes (agent help vs report), the blocking
// /help/assist spawn with its loading/error swap, /help/report with the
// Sentry event-id confirmation, and the sticky remote-access checkbox. The
// open_help channel message stages a pending launch here (via
// setPendingHelpLaunch) before the shell routes to /help.

export interface HelpLaunchContext {
  workspaceAgentId: string;
  isAssistAvailable: boolean;
  description: string;
  isAgentReport: boolean;
  workspaceName: string;
}

const EMPTY_LAUNCH: HelpLaunchContext = {
  workspaceAgentId: "",
  isAssistAvailable: false,
  description: "",
  isAgentReport: false,
  workspaceName: "",
};

// Staged by the open_help handler just before routing to /help; consumed
// exactly once, by the page that opens (or is already open) for its machine.
let pendingLaunch: HelpLaunchContext | null = null;

export function setPendingHelpLaunch(launch: Partial<HelpLaunchContext>): void {
  pendingLaunch = { ...EMPTY_LAUNCH, ...launch };
}

export function takePendingHelpLaunch(): HelpLaunchContext | null {
  const taken = pendingLaunch;
  pendingLaunch = null;
  return taken;
}

/** The machine a staged launch is for ("" for none), or null when nothing is
 * staged. Does not consume it. */
export function stagedHelpLaunchWorkspace(): string | null {
  return pendingLaunch?.workspaceAgentId ?? null;
}

/** Whether a launch is staged for the /help route naming `routeWorkspace`
 * (undefined for a route that names no machine). Does not consume it. */
export function isHelpLaunchStagedFor(
  routeWorkspace: string | undefined,
): boolean {
  return (
    pendingLaunch !== null && isLaunchForRoute(pendingLaunch, routeWorkspace)
  );
}

function isLaunchForRoute(
  launch: HelpLaunchContext,
  routeWorkspace: string | undefined,
): boolean {
  return launch.workspaceAgentId === (routeWorkspace ?? "");
}

/** The /help route's own query params, as read off the route (absent = undefined). */
export interface HelpRouteParams {
  workspace?: string;
  assist?: string;
  description?: string;
  agent_report?: string;
  workspace_name?: string;
}

/**
 * Settle what the arriving /help page launches with: the route's params over
 * the launch staged for it, field by field. A route that names no machine and
 * carries no report of its own, with nothing staged, leaves the page on its
 * defaults.
 *
 * Field by field rather than whole: the agent-report flow supplies both
 * halves. `?workspace=` has to be in the route because it is the only thing
 * that keeps that machine mounted behind the modal, while the diagnosis is far
 * too large for a URL and rides the staged launch -- so a route that names the
 * machine must not blank the description staged alongside it.
 *
 * Only the launch staged for THIS machine, though: a launch naming another one
 * is not this route's, and merging it would address the form (and the logs and
 * transcript the report collects) to the machine the route names while filling
 * it with what the other machine said. A route naming no machine takes only a
 * launch that names none either.
 */
export function stageHelpLaunchFromRoute(params: HelpRouteParams): void {
  const stagedForAnyMachine = takePendingHelpLaunch();
  const staged =
    stagedForAnyMachine !== null &&
    isLaunchForRoute(stagedForAnyMachine, params.workspace)
      ? stagedForAnyMachine
      : null;
  if (
    staged === null &&
    !params.workspace &&
    !params.description &&
    !params.agent_report
  )
    return;
  const base = staged ?? EMPTY_LAUNCH;
  setPendingHelpLaunch({
    workspaceAgentId: params.workspace ?? base.workspaceAgentId,
    isAssistAvailable:
      params.assist === undefined
        ? base.isAssistAvailable
        : params.assist === "1",
    description: params.description ?? base.description,
    isAgentReport:
      params.agent_report === undefined
        ? base.isAgentReport
        : params.agent_report === "1",
    workspaceName: params.workspace_name ?? base.workspaceName,
  });
}

const STICKY_REMOTE_ACCESS_KEY = "minds.help.help-remote-access";
const STICKY_INCLUDE_LOGS_KEY = "minds.help.help-include-logs";
const STICKY_INCLUDE_TRANSCRIPT_KEY = "minds.help.help-include-transcript";

export type HelpMode = "agent" | "report";
export type HelpPhase = "form" | "agent_loading" | "agent_error" | "sent";

interface FetchLike {
  (url: string, init?: RequestInit): Promise<Response>;
}

export interface HelpModelOptions {
  fetcher?: FetchLike;
  onClose?: () => void;
  redraw?: () => void;
  storage?: Pick<Storage, "getItem" | "setItem">;
  /** Injectable clipboard write, for tests; defaults to navigator.clipboard. */
  clipboardWrite?: (text: string) => Promise<void>;
}

// How long the report-ID chip flashes its copied confirmation, matching the
// share-link chip's flash.
const COPY_FLASH_MS = 1200;

export class HelpModel {
  launch: HelpLaunchContext = EMPTY_LAUNCH;
  mode: HelpMode = "report";
  phase: HelpPhase = "form";
  description = "";
  isRemoteAccessAllowed = false;
  isLogsIncluded = true;
  isTranscriptIncluded = true;
  statusMessage: string | null = null;
  isStatusError = false;
  agentErrorMessage = "";
  /** The refusing machine's own words, shown under `agentErrorMessage`. */
  agentErrorDetail = "";
  sentEventId: string | null = null;
  isReportIdCopied = false;
  private copyFlashTimer: ReturnType<typeof setTimeout> | null = null;
  isSubmitBusy = false;
  private isReportInFlight = false;
  private isSendFailed = false;

  private readonly options: HelpModelOptions;

  constructor(options: HelpModelOptions = {}) {
    this.options = options;
    const staged = takePendingHelpLaunch();
    if (staged !== null) this.launch = staged;
    this.description = this.launch.description;
    // Agent help is the default when available -- except for an /assist
    // agent's escalated diagnosis (pre-filled description), which must land
    // on the report form for a human to review (legacy parity).
    this.mode =
      this.launch.isAssistAvailable && !this.launch.description
        ? "agent"
        : "report";
    const stored = this.storage().getItem(STICKY_REMOTE_ACCESS_KEY);
    if (stored !== null) this.isRemoteAccessAllowed = stored === "true";
    const storedLogs = this.storage().getItem(STICKY_INCLUDE_LOGS_KEY);
    if (storedLogs !== null) this.isLogsIncluded = storedLogs === "true";
    const storedTranscript = this.storage().getItem(
      STICKY_INCLUDE_TRANSCRIPT_KEY,
    );
    if (storedTranscript !== null)
      this.isTranscriptIncluded = storedTranscript === "true";
  }

  private fetcher(): FetchLike {
    return (
      this.options.fetcher ??
      ((url, init) => fetch(url, { credentials: "same-origin", ...init }))
    );
  }

  private storage(): Pick<Storage, "getItem" | "setItem"> {
    return this.options.storage ?? localStorage;
  }

  private redraw(): void {
    this.options.redraw?.();
  }

  /** On the form with nothing being sent, no failed send on screen, and
   * nothing written over what the launch filled in: the one state the page
   * may swap for a newly arrived report without losing what the user acted on. */
  get isAwaitingInput(): boolean {
    return (
      this.phase === "form" &&
      !this.isReportInFlight &&
      !this.isSendFailed &&
      this.description === this.launch.description
    );
  }

  close(): void {
    this.options.onClose?.();
  }

  setRemoteAccessAllowed(value: boolean): void {
    this.isRemoteAccessAllowed = value;
    this.storage().setItem(STICKY_REMOTE_ACCESS_KEY, value ? "true" : "false");
  }

  setLogsIncluded(value: boolean): void {
    this.isLogsIncluded = value;
    this.storage().setItem(STICKY_INCLUDE_LOGS_KEY, value ? "true" : "false");
  }

  setTranscriptIncluded(value: boolean): void {
    this.isTranscriptIncluded = value;
    this.storage().setItem(
      STICKY_INCLUDE_TRANSCRIPT_KEY,
      value ? "true" : "false",
    );
  }

  async copyReportId(): Promise<void> {
    if (this.sentEventId === null) return;
    try {
      await this.clipboardWrite(this.sentEventId);
    } catch {
      // The ID is on screen and quotable either way; no confirmation flash.
      return;
    }
    this.isReportIdCopied = true;
    if (this.copyFlashTimer !== null) clearTimeout(this.copyFlashTimer);
    this.copyFlashTimer = setTimeout(() => {
      this.copyFlashTimer = null;
      this.isReportIdCopied = false;
      this.redraw();
    }, COPY_FLASH_MS);
    this.redraw();
  }

  private clipboardWrite(text: string): Promise<void> {
    if (this.options.clipboardWrite) return this.options.clipboardWrite(text);
    const clipboard = navigator.clipboard;
    if (!clipboard) return Promise.reject(new Error("clipboard unavailable"));
    return clipboard.writeText(text);
  }

  backToReportFromError(): void {
    this.phase = "form";
    this.mode = "report";
    this.isSubmitBusy = false;
    this.redraw();
  }

  async submit(): Promise<void> {
    // A second click (or Enter) while a submission is already in flight must
    // not fire a duplicate request.
    if (this.isSubmitBusy) return;
    const description = this.description.trim();
    if (!description) {
      this.statusMessage = "Please describe the problem first.";
      this.isStatusError = true;
      this.redraw();
      return;
    }
    if (this.mode === "agent" && !this.launch.isAgentReport) {
      await this.submitAgentHelp(description);
      return;
    }
    await this.submitReport(description);
  }

  private async submitAgentHelp(description: string): Promise<void> {
    this.isSubmitBusy = true;
    this.phase = "agent_loading";
    this.redraw();
    try {
      const response = await this.fetcher()("/help/assist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          workspace_agent_id: this.launch.workspaceAgentId,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        detail?: string;
      };
      if (response.ok) {
        // The chat exists and its tab already auto-opened in the workspace.
        this.close();
      } else {
        this.phase = "agent_error";
        this.agentErrorMessage = data.error ?? "Could not start an agent.";
        this.agentErrorDetail = data.detail ?? "";
      }
    } catch {
      this.phase = "agent_error";
      this.agentErrorMessage = "Network error starting the agent.";
      this.agentErrorDetail = "";
    } finally {
      this.isSubmitBusy = false;
      this.redraw();
    }
  }

  private async submitReport(description: string): Promise<void> {
    this.isSubmitBusy = true;
    this.isReportInFlight = true;
    this.isSendFailed = false;
    this.statusMessage = "Sending...";
    this.isStatusError = false;
    this.redraw();
    try {
      const response = await this.fetcher()("/help/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          remote_access: this.isRemoteAccessAllowed,
          workspace_agent_id: this.launch.workspaceAgentId,
          include_logs: this.isLogsIncluded,
          include_transcript: this.isTranscriptIncluded,
        }),
      });
      const data = (await response.json().catch(() => ({}))) as {
        error?: string;
        event_id?: string | null;
      };
      if (response.ok) {
        this.phase = "sent";
        this.sentEventId = data.event_id ?? null;
        this.statusMessage = null;
      } else {
        this.statusMessage = data.error ?? "Could not send the report.";
        this.isStatusError = true;
        this.isSendFailed = true;
      }
    } catch {
      this.statusMessage = "Network error sending the report.";
      this.isStatusError = true;
      this.isSendFailed = true;
    } finally {
      this.isSubmitBusy = false;
      this.isReportInFlight = false;
      this.redraw();
    }
  }
}
