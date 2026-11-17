// Which of the home page's tabs is up (specs/home-screen-redesign, 4.3): the
// choice is kept per window in sessionStorage, so a window comes back to the
// tab it was on and another window keeps its own.

export type HomeTab = "workspaces" | "apps" | "invitations";

const STORAGE_KEY = "home-tab";
const TABS: readonly HomeTab[] = ["workspaces", "apps", "invitations"];

export function isHomeTab(value: unknown): value is HomeTab {
  return typeof value === "string" && (TABS as readonly string[]).includes(value);
}

/** The remembered tab, or Workspaces when none is remembered or storage is unavailable. */
export function readRememberedHomeTab(): HomeTab {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    return isHomeTab(stored) ? stored : "workspaces";
  } catch {
    return "workspaces";
  }
}

export function rememberHomeTab(tab: HomeTab): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, tab);
  } catch {
    // A window with no storage simply opens on Workspaces next time.
  }
}
