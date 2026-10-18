// Page-level access to the app singletons (stores + shell). The router mounts
// page components without attrs, so pages reach shared state through this
// registry; index.ts registers the context once at boot, before mounting.
//
// Generic on purpose: every page tranche imports from here rather than
// inventing its own plumbing.

import type { AppStores } from "./models/boot";
import type { ShellState } from "./views/shell/shell-state";

export interface AppContext {
  stores: AppStores;
  shell: ShellState;
}

let registered: AppContext | null = null;

export function registerAppContext(context: AppContext): void {
  registered = context;
}

export function getAppContext(): AppContext {
  if (registered === null) {
    throw new Error("App context read before boot registered it (index.ts must call registerAppContext first)");
  }
  return registered;
}

// Test-only: reset between vitest cases.
export function clearAppContextForTests(): void {
  registered = null;
}
