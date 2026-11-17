// The per-workspace images the home page draws (specs/home-screen-redesign,
// 4.10 and 4.12): where an app's icon and a workspace's wallpaper are served,
// and the refresh that fetches a loaded workspace's wallpaper.

import type { UiWorkspaceApp } from "../generated/ui";

const BASE = "/ui/api/workspaces";

/** The served icon of an own app, or null when the app has no usable icon (a monogram stands in). */
export function appIconUrl(agentId: string, app: UiWorkspaceApp): string | null {
  const version = app.icon_version ?? "";
  if (version === "") return null;
  return `${BASE}/${encodeURIComponent(agentId)}/apps/${encodeURIComponent(app.name)}/icon.svg?v=${encodeURIComponent(version)}`;
}

/** The cached wallpaper of a workspace, or null when none is cached (the card is its accent tile). */
export function wallpaperUrl(agentId: string, wallpaperVersion: string): string | null {
  if (wallpaperVersion === "") return null;
  return `${BASE}/${encodeURIComponent(agentId)}/wallpaper?v=${encodeURIComponent(wallpaperVersion)}`;
}

/** Ask the desktop to read the workspace's wallpaper now that the workspace is up; fire-and-forget. */
export function requestWallpaperRefresh(
  agentId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  return fetchImpl(`${BASE}/${encodeURIComponent(agentId)}/wallpaper/refresh`, {
    method: "POST",
    credentials: "same-origin",
  }).then(
    (response) => {
      if (!response.ok) console.warn(`[wallpaper] the refresh of ${agentId} was refused: HTTP ${response.status}`);
    },
    (error: unknown) => {
      console.warn(`[wallpaper] could not ask for a refresh of ${agentId}: ${String(error)}`);
    },
  );
}
