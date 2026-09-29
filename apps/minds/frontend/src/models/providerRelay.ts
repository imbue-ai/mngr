/**
 * Arms the desktop's relay for a workspace's provider sign-in (POST /ui/api/provider-relay).
 *
 * The backend validates the URL, listens on its loopback callback port, and opens the page in the
 * browser Settings names. Any failure -- a refused URL, a port it cannot bind, no reachable
 * backend -- reads as "cannot relay", which sends the workspace to its manual sign-in.
 */
export async function armProviderRelay(
  workspaceId: string,
  flowId: string,
  url: string,
): Promise<boolean> {
  try {
    const response = await fetch("/ui/api/provider-relay", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: workspaceId, flow_id: flowId, url }),
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { relay?: unknown };
    return body.relay === true;
  } catch {
    return false;
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
