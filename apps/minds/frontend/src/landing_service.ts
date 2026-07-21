// Landing-page services: the backup-status fetch fan-out (ported from the
// deleted Landing.jinja inline ``loadBackupStatus``) and the workspace / sync
// action requests. Module state + m.redraw via the store's notify pattern;
// ``fetch`` is injectable so vitest can exercise the flows without a network.
import m from "mithril";

import { beginMindAction, completeMindAction, failMindAction } from "./store";

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

let fetchImpl: FetchLike = (url, init) => fetch(url, init);

export function setFetchForTesting(replacement: FetchLike | null): void {
  fetchImpl = replacement ?? ((url, init) => fetch(url, init));
}

// -- Backup badges ----------------------------------------------------------

export interface BackupBadgeStatus {
  state: "CHECKING" | "BACKING_UP" | "BACKED_UP" | "CREATED_RECENTLY" | "NONE";
  // Newest snapshot time (BACKED_UP) as ISO.
  lastSuccessAt: string | null;
  // Row create time (CREATED_RECENTLY / NONE fallback) as ISO.
  createdAt: string | null;
}

const BACKUP_RECENT_CREATE_MS = 75 * 60 * 1000;

let backupStatusByAgentId: Record<string, BackupBadgeStatus> = {};
// Export-in-flight guard + transient error text per agent id.
let exportStateByAgentId: Record<string, "exporting" | "failed"> = {};

export function getBackupBadge(agentId: string): BackupBadgeStatus | null {
  return backupStatusByAgentId[agentId] ?? null;
}

export function getExportState(agentId: string): "exporting" | "failed" | null {
  return exportStateByAgentId[agentId] ?? null;
}

export function resetLandingServiceForTesting(): void {
  backupStatusByAgentId = {};
  exportStateByAgentId = {};
  fetchImpl = (url, init) => fetch(url, init);
}

interface BackupsResponse {
  snapshots?: Array<{ time: string }>;
  is_backing_up?: boolean;
}

// Derive the badge status from a workspace's snapshots (+ its create time for
// the no-backups fallback). "Has snapshots" -> BACKED_UP (last success =
// newest snapshot's time); a backup running right now takes precedence; no
// snapshots falls back to "Created N ago" within 75 minutes of creation,
// else "No backups".
function deriveBackupStatus(data: BackupsResponse | null, createdAt: string | null): BackupBadgeStatus {
  const snapshots = data?.snapshots ?? [];
  let latestTime: string | null = null;
  snapshots.forEach((snapshot) => {
    if (latestTime === null || Date.parse(snapshot.time) >= Date.parse(latestTime)) {
      latestTime = snapshot.time;
    }
  });
  if (data?.is_backing_up === true) {
    return { state: "BACKING_UP", lastSuccessAt: latestTime, createdAt };
  }
  if (latestTime !== null) {
    return { state: "BACKED_UP", lastSuccessAt: latestTime, createdAt };
  }
  const createdMs = createdAt !== null ? Date.parse(createdAt) : Number.NaN;
  if (!Number.isNaN(createdMs) && Date.now() - createdMs < BACKUP_RECENT_CREATE_MS) {
    return { state: "CREATED_RECENTLY", lastSuccessAt: null, createdAt };
  }
  return { state: "NONE", lastSuccessAt: null, createdAt };
}

// Fetch the workspace list once (for each row's create time), then fan out
// one backups request per rendered row -- cross-workspace parallelism lives
// here in the frontend; the backend route is strictly per-workspace.
export function loadBackupStatus(rowAgentIds: string[]): void {
  if (rowAgentIds.length === 0) return;
  rowAgentIds.forEach((agentId) => {
    backupStatusByAgentId[agentId] = { state: "CHECKING", lastSuccessAt: null, createdAt: null };
  });
  m.redraw();
  void fetchImpl("/api/v1/workspaces")
    .then((response) => (response.ok ? (response.json() as Promise<{ workspaces?: Array<Record<string, string>> }>) : null))
    .catch(() => null)
    .then((workspacesData) => {
      const createTimeByAgentId: Record<string, string> = {};
      (workspacesData?.workspaces ?? []).forEach((workspace) => {
        const agentId = workspace["agent_id"] ?? workspace["id"];
        const createTime = workspace["create_time"] ?? workspace["created_at"];
        if (agentId !== undefined && createTime !== undefined) createTimeByAgentId[agentId] = createTime;
      });
      rowAgentIds.forEach((agentId) => {
        void fetchImpl(`/api/v1/workspaces/${encodeURIComponent(agentId)}/backups`)
          .then((response) => (response.ok ? (response.json() as Promise<BackupsResponse>) : null))
          .catch(() => null)
          .then((data) => {
            backupStatusByAgentId[agentId] = deriveBackupStatus(data, createTimeByAgentId[agentId] ?? null);
            m.redraw();
          });
      });
    });
}

// Download the newest backup as a zip. The export route passes the snapshot
// id to restic verbatim, so restic's own 'latest' addressing exports the
// newest snapshot without listing them first. In-flight requests are ignored;
// failures show a transient "export failed" for 3 seconds.
export function exportLatestBackup(agentId: string): void {
  if (exportStateByAgentId[agentId] === "exporting") return;
  exportStateByAgentId[agentId] = "exporting";
  m.redraw();
  void fetchImpl(`/api/v1/workspaces/${encodeURIComponent(agentId)}/backups/latest/export`, { method: "POST" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`export failed: ${response.status}`);
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const match = /filename="?([^"]+)"?/.exec(disposition);
      const name = match !== null ? match[1] : `${agentId}-backup.zip`;
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      delete exportStateByAgentId[agentId];
      m.redraw();
    })
    .catch(() => {
      exportStateByAgentId[agentId] = "failed";
      m.redraw();
      window.setTimeout(() => {
        if (exportStateByAgentId[agentId] === "failed") {
          delete exportStateByAgentId[agentId];
          m.redraw();
        }
      }, 3000);
    });
}

// -- Mind start / stop ------------------------------------------------------

// The endpoints are synchronous: they resolve only once the host has actually
// started/stopped (or failed), so a non-ok response is a real failure to
// revert. The optimistic transient + interim-payload guard live in the store.
export function startMind(agentId: string): void {
  beginMindAction(agentId, "RUNNING");
  void fetchImpl(`/api/v1/workspaces/${encodeURIComponent(agentId)}/start`, { method: "POST" })
    .then((response) => {
      if (response.ok) completeMindAction(agentId);
      else failMindAction(agentId);
    })
    .catch(() => failMindAction(agentId));
}

export function stopMind(agentId: string): void {
  beginMindAction(agentId, "STOPPED");
  void fetchImpl(`/api/v1/workspaces/${encodeURIComponent(agentId)}/stop`, { method: "POST" })
    .then((response) => {
      if (response.ok) completeMindAction(agentId);
      else failMindAction(agentId);
    })
    .catch(() => failMindAction(agentId));
}

// -- Provider toggle --------------------------------------------------------

export interface ProviderToggleFailure {
  status: number;
  message: string;
}

// PATCH the provider's enabled flag. Resolves null on success; a failure
// (e.g. 409 when disabling a provider that still has active workspaces)
// resolves with the server's reason so the caller can surface it.
export function patchProviderEnabled(name: string, isEnabled: boolean): Promise<ProviderToggleFailure | null> {
  return fetchImpl(`/api/v1/desktop/providers/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: isEnabled }),
  })
    .then(async (response) => {
      if (response.ok) return null;
      const body = (await response.json().catch(() => null)) as { error?: string; detail?: string } | null;
      return { status: response.status, message: body?.error ?? body?.detail ?? `HTTP ${response.status}` };
    })
    .catch((error: unknown) => ({ status: 0, message: String(error) }));
}

// -- Sync unlock + remote record removal ------------------------------------

// POST the master password; resolves null on success, else the error text.
export function submitSyncUnlock(password: string): Promise<string | null> {
  return fetchImpl("/_chrome/sync-unlock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  })
    .then(async (response) => {
      const data = (await response.json().catch(() => null)) as { ok?: boolean; error?: string } | null;
      if (response.status === 200 && data?.ok === true) return null;
      return data?.error ?? "That password did not unlock any account.";
    })
    .catch(() => "The unlock request failed (network error).");
}

// Remove a synced record (a remote row's X). Resolves whether it succeeded.
export function removeRemoteRecord(hostId: string): Promise<boolean> {
  return fetchImpl("/_chrome/workspaces/remove-record", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ host_id: hostId }),
  })
    .then((response) => response.ok)
    .catch(() => false);
}

// -- Relative time ----------------------------------------------------------

export function relativeAgo(iso: string | null): string {
  if (iso === null || iso === "") return "—";
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
