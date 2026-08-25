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

// Staged by the open_help handler (and, once wired, the titlebar's help
// button) just before routing to /help; consumed exactly once on page init.
let pendingLaunch: HelpLaunchContext | null = null;

export function setPendingHelpLaunch(launch: Partial<HelpLaunchContext>): void {
  pendingLaunch = { ...EMPTY_LAUNCH, ...launch };
}

export function takePendingHelpLaunch(): HelpLaunchContext | null {
  const taken = pendingLaunch;
  pendingLaunch = null;
  return taken;
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
}

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
  sentEventId: string | null = null;
  isSubmitBusy = false;
  /** True only while a report POST is in flight. Narrower than isSubmitBusy
   * (which also covers the assist spawn, whose success closes the surface
   * itself): only a report in flight pins the surface open. */
  private isReportInFlight = false;

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
      };
      if (response.ok) {
        // The chat exists and its tab already auto-opened in the workspace.
        this.close();
      } else {
        this.phase = "agent_error";
        this.agentErrorMessage = data.error ?? "Could not start an agent.";
      }
    } catch {
      this.phase = "agent_error";
      this.agentErrorMessage = "Network error starting the agent.";
    } finally {
      this.isSubmitBusy = false;
      this.redraw();
    }
  }

  private async submitReport(description: string): Promise<void> {
    this.isSubmitBusy = true;
    this.isReportInFlight = true;
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
      }
    } catch {
      this.statusMessage = "Network error sending the report.";
      this.isStatusError = true;
    } finally {
      // Either the sent phase (with the report id) or the error is now shown,
      // so the surface unpins and the Done / close paths work again.
      this.isSubmitBusy = false;
      this.isReportInFlight = false;
      this.redraw();
    }
  }
}
