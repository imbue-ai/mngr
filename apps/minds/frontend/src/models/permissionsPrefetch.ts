// Warms a machine's permissions overview before its pane is opened.
//
// The pane reads on its first mount, so the read started when the panel
// opened and "Loading permissions..." was however long that read took. The
// read itself is quick; it was simply started late. Pointing at the key that
// opens the pane starts it instead, so the click usually lands on an answer
// that is already here.
//
// Deliberately NOT warmed on entering a machine: the overview is answered by
// asking that machine's latchkey gateway what it holds, and a machine is
// entered far more often than its permissions are read.

import type { UiWorkspacePermissions } from "../generated/ui";

export interface FetchJsonResult {
  ok: boolean;
  status: number;
  body: unknown;
}

export type FetchJsonLike = (url: string) => Promise<FetchJsonResult>;

/**
 * How long a warm may answer for. The gap it covers is pointing at the key and
 * clicking it, so this only has to outlive a hand; past that the pane is better
 * off reading for itself, since what it shows (a grant, a pending request)
 * changes underneath.
 */
const WARM_TTL_MS = 15_000;

/** One machine's warm: a window shows one panel, so a second warm replaces it. */
let warmed: { agentId: string; startedAt: number; pending: Promise<FetchJsonResult> } | null = null;

export function permissionsOverviewUrl(agentId: string): string {
  return `/ui/api/workspaces/${encodeURIComponent(agentId)}/permissions`;
}

/** Start reading `agentId`'s overview unless a live warm already covers it. */
export function warmPermissionsOverview(agentId: string, fetchJson: FetchJsonLike): void {
  if (warmed !== null && warmed.agentId === agentId && !isExpired(warmed.startedAt)) return;
  warmed = {
    agentId,
    startedAt: Date.now(),
    // A warm has no surface of its own: a failure is carried in the result and
    // reported by the pane that spends it, exactly as its own read would be.
    pending: fetchJson(permissionsOverviewUrl(agentId)),
  };
}

/** The warm for `agentId`, or null when there is none live for it. */
export function readWarmedPermissionsOverview(agentId: string): Promise<FetchJsonResult> | null {
  if (warmed === null || warmed.agentId !== agentId || isExpired(warmed.startedAt)) return null;
  return warmed.pending;
}

/** Drop the warm: its answer is spent, or something changed it. */
export function forgetWarmedPermissionsOverview(): void {
  warmed = null;
}

function isExpired(startedAt: number): boolean {
  return Date.now() - startedAt > WARM_TTL_MS;
}

/** The shape the pane expects back, named here so the warm and the pane agree. */
export type WarmedPermissions = UiWorkspacePermissions;
