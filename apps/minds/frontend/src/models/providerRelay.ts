/** Whether the desktop is relaying a sign-in, and when it is not, why not if it said. */
export interface ProviderRelayAnswer {
  relay: boolean;
  reason?: string;
}

/**
 * Arms the desktop's relay for a workspace's provider sign-in (POST /ui/api/provider-relay).
 *
 * The backend validates the URL, listens on its loopback callback port, and opens the page in the
 * browser Settings names. Any failure -- a refused URL, a port it cannot bind, no reachable
 * backend -- reads as "cannot relay". A refusal the backend answered carries its reason, so the
 * workspace can start again on a fresh port when the port was taken; otherwise it signs in
 * manually.
 */
export async function armProviderRelay(
  workspaceId: string,
  flowId: string,
  url: string,
): Promise<ProviderRelayAnswer> {
  try {
    const response = await fetch("/ui/api/provider-relay", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: workspaceId, flow_id: flowId, url }),
    });
    if (!response.ok) return { relay: false };
    const body = (await response.json()) as { relay?: unknown; reason?: unknown };
    if (body.relay === true) return { relay: true };
    return typeof body.reason === "string" ? { relay: false, reason: body.reason } : { relay: false };
  } catch {
    return { relay: false };
  }
}

/** Stop the relay for a workspace's sign-in that has ended (POST /ui/api/provider-relay/stop). */
export function stopProviderRelay(workspaceId: string, flowId: string): void {
  void fetch("/ui/api/provider-relay/stop", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: workspaceId, flow_id: flowId }),
  }).catch(() => undefined);
}
