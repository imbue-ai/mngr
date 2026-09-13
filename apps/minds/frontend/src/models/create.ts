// T1 models: create-area API types + the page state machines that used to
// live in creating.js / the Create form's inline script. Views stay thin;
// everything testable lives here.

import m from "mithril";

// ---- /ui/api/create response shapes (hand-typed: the per-area /ui/api
// bodies are not part of the generated channel schema yet). Keep in sync
// with ui_api_create.py.

export interface CreateAccountOption {
  user_id: string;
  email: string;
}

export interface CloudAccountOption {
  name: string;
  alias: string;
  backend: string;
  region: string;
}

export interface CreateRetryPrefill {
  git_url: string;
  branch: string;
  host_name: string;
  launch_mode: string;
  docker_runtime: string;
  backup_provider: string;
  backup_api_key_env: string;
  account_id: string;
  region: string;
  cloud_account: string;
  instance_type: string;
  color: string;
}

export interface CreateFormDefaults {
  accounts: CreateAccountOption[];
  default_account_id: string;
  launch_modes: string[];
  selected_launch_mode: string;
  docker_runtimes: string[];
  selected_docker_runtime: string;
  backup_providers: string[];
  selected_backup_provider: string;
  region_options_by_launch_mode: Record<string, string[]>;
  region_selected_by_launch_mode: Record<string, string>;
  instance_types_by_backend: Record<string, [string, string][]>;
  default_instance_type_by_backend: Record<string, string>;
  cloud_accounts: CloudAccountOption[];
  byok_clouds_enabled: boolean;
  git_url: string;
  branch: string;
  color: string;
  prefill: CreateRetryPrefill | null;
}

export interface LandingExtras {
  destroying_status_by_agent_id: Record<string, string>;
  locked_account_emails: string[];
  is_discovery_complete: boolean;
  has_restorable_workspaces: boolean;
}

export interface OnboardingCloudApp {
  icon: string;
  name: string;
}

export interface LiveCreateAttemptDetail {
  workspace_name: string;
  provider_label: string;
  is_remote: boolean;
  expected_duration_seconds: number;
  onboarding_services: OnboardingCloudApp[];
}

export interface CreateAttemptDetail {
  kind: "live" | "record" | "gone";
  live: LiveCreateAttemptDetail | null;
  record: {
    state: "interrupted" | "failed";
    workspace_name: string;
    error: string | null;
    error_kind: string | null;
    log_tail: string[];
    provider_label: string;
  } | null;
}

export async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`GET ${url} failed (${response.status})`);
  return (await response.json()) as T;
}

export function fetchCreateFormDefaults(retryId: string | null): Promise<CreateFormDefaults> {
  const suffix = retryId ? `?retry=${encodeURIComponent(retryId)}` : "";
  return fetchJson<CreateFormDefaults>(`/ui/api/create/form-defaults${suffix}`);
}

export function fetchLandingExtras(): Promise<LandingExtras> {
  return fetchJson<LandingExtras>("/ui/api/create/landing-extras");
}

export function fetchCreateAttemptDetail(createAttemptId: string): Promise<CreateAttemptDetail> {
  return fetchJson<CreateAttemptDetail>(`/ui/api/create/attempts/${encodeURIComponent(createAttemptId)}`);
}

// ---- Name validation (port of the Create form's live host-name rules).

export function hostNameFormatError(value: string): string {
  if (value === "") return "";
  const invalidChar = value.match(/[^a-zA-Z0-9_-]/);
  if (invalidChar) {
    if (invalidChar[0] === ".") return "Dots aren't allowed in a name.";
    if (invalidChar[0] === " ") return "Spaces aren't allowed in a name.";
    return "Use only letters, numbers, dashes, and underscores.";
  }
  if (/^[-_]/.test(value)) return "Can't start with a dash or underscore.";
  if (/[-_]$/.test(value)) return "Can't end with a dash or underscore.";
  return "";
}

/** What the Recovery deep-link should dispatch on arrival, if anything.
 *
 * "start" opens a stopped machine: an idempotent ``mngr start``, which has
 * nothing to bounce. "restart" is the user asking for the full stop+start.
 * Only the first is safe to probe alongside, since a bounce would tear the
 * container down under the probe. */
export type RecoveryIntent = "start" | "restart" | null;

/** The Recovery deep-link for a workspace. */
export function recoveryRoute(agentId: string, returnTo: string, intent: RecoveryIntent): string {
  const intentParam = intent === null ? "" : `&intent=${intent}`;
  return `/agents/${encodeURIComponent(agentId)}/recovery?return_to=${encodeURIComponent(returnTo)}${intentParam}`;
}

// ---- Creating-page progress (port of creating.js's time-eased bar).

export function progressForElapsed(elapsedSeconds: number, expectedDurationSeconds: number): number {
  const T = expectedDurationSeconds > 0 ? expectedDurationSeconds : 60;
  if (elapsedSeconds <= T) return 80 * (elapsedSeconds / T);
  return 80 + 20 * (1 - Math.exp(-(elapsedSeconds - T) / T));
}

// ---- Optimistic mind Start/Stop tracking (port of the Landing page's
// pendingMindActionByAgent machinery). The channel's workspaces message
// carries the authoritative liveness -- including backend-observed
// STARTING/STOPPING transitions (e.g. a stop issued from another device),
// which pass straight through; while a local action is in flight the same
// transient labels win until the target state arrives.

export type MindLiveness = "RUNNING" | "STOPPED" | "UNKNOWN" | "STARTING" | "STOPPING";

// Human labels for the non-RUNNING liveness states, shared by every surface
// that renders a liveness badge or title (RUNNING deliberately has none:
// those surfaces show no badge for a running machine).
export const MIND_LIVENESS_LABELS: Record<string, string> = {
  STOPPED: "Stopped",
  STOPPING: "Stopping…",
  STARTING: "Starting…",
  UNKNOWN: "Status unknown",
};

export class MindLivenessTracker {
  private pendingTargetByAgentId = new Map<string, "RUNNING" | "STOPPED">();
  // Injected in tests; defaults to m.redraw (which needs a mounted root).
  private readonly redraw: () => void;

  constructor(redraw?: () => void) {
    this.redraw = redraw ?? (() => m.redraw());
  }

  displayedLiveness(agentId: string, authoritative: string): MindLiveness {
    const pending = this.pendingTargetByAgentId.get(agentId);
    if (pending !== undefined) {
      if (authoritative === pending) {
        this.pendingTargetByAgentId.delete(agentId);
        return pending;
      }
      return pending === "RUNNING" ? "STARTING" : "STOPPING";
    }
    if (
      authoritative === "RUNNING" ||
      authoritative === "STOPPED" ||
      authoritative === "STOPPING" ||
      authoritative === "STARTING"
    ) {
      return authoritative;
    }
    return "UNKNOWN";
  }

  async start(agentId: string): Promise<boolean> {
    return await this.runAction(agentId, "RUNNING", `/api/v1/workspaces/${encodeURIComponent(agentId)}/start`);
  }

  async stop(agentId: string): Promise<boolean> {
    return await this.runAction(agentId, "STOPPED", `/api/v1/workspaces/${encodeURIComponent(agentId)}/stop`);
  }

  private async runAction(agentId: string, target: "RUNNING" | "STOPPED", url: string): Promise<boolean> {
    this.pendingTargetByAgentId.set(agentId, target);
    this.redraw();
    let isOk: boolean;
    try {
      const response = await fetch(url, { method: "POST", credentials: "same-origin" });
      isOk = response.ok;
    } catch {
      isOk = false;
    }
    if (!isOk) this.pendingTargetByAgentId.delete(agentId);
    this.redraw();
    return isOk;
  }
}

// ---- Creating-page watcher: status polling (authoritative) + the op-log
// SSE (live log lines only), port of creating.js. The op-log streams stay
// SSE by decision; only the chrome events moved to the channel.

export interface CreateOperationStatus {
  status?: string;
  status_text?: string;
  redirect_url?: string;
  error?: string;
  error_kind?: string;
}

export interface CreateAttemptWatcherHooks {
  onDone(redirectUrl: string): void;
  onFailed(error: string, errorKind: string): void;
  onStageText(text: string): void;
  onLogLines(lines: string[]): void;
}

export class CreateAttemptWatcher {
  readonly createAttemptId: string;
  private readonly hooks: CreateAttemptWatcherHooks;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private source: EventSource | null = null;
  private isStopped = false;

  constructor(createAttemptId: string, hooks: CreateAttemptWatcherHooks) {
    this.createAttemptId = createAttemptId;
    this.hooks = hooks;
  }

  start(): void {
    this.pollOnce();
    this.pollTimer = setInterval(() => this.pollOnce(), 2000);
    this.source = new EventSource(
      `/api/v1/workspaces/operations/create/${encodeURIComponent(this.createAttemptId)}/logs`,
    );
    this.source.onmessage = (event) => {
      let data: { done?: boolean; log?: string };
      try {
        data = JSON.parse(event.data as string) as { done?: boolean; log?: string };
      } catch {
        return;
      }
      if (data.done) {
        this.source?.close();
        this.source = null;
      } else if (typeof data.log === "string") {
        this.hooks.onLogLines([data.log]);
      }
    };
    this.source.onerror = () => {
      this.source?.close();
      this.source = null;
    };
  }

  stop(): void {
    this.isStopped = true;
    if (this.pollTimer !== null) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.source?.close();
    this.source = null;
  }

  applyStatus(data: CreateOperationStatus | null): void {
    if (this.isStopped || data === null) return;
    if (data.status === "DONE" && data.redirect_url) {
      if (this.pollTimer !== null) clearInterval(this.pollTimer);
      this.pollTimer = null;
      this.hooks.onDone(data.redirect_url);
    } else if (data.status === "FAILED") {
      if (this.pollTimer !== null) clearInterval(this.pollTimer);
      this.pollTimer = null;
      this.hooks.onFailed(data.error ?? "unknown error", data.error_kind ?? "");
    } else if (data.status_text) {
      this.hooks.onStageText(data.status_text);
    }
  }

  private pollOnce(): void {
    fetch(`/api/v1/workspaces/operations/create/${encodeURIComponent(this.createAttemptId)}`, {
      credentials: "same-origin",
    })
      .then((response) => (response.ok ? (response.json() as Promise<CreateOperationStatus>) : null))
      .then((data) => {
        this.applyStatus(data);
        m.redraw();
      })
      .catch(() => undefined);
  }
}
