import * as appRegistry from './registry.app.js';
import * as chromeRegistry from './registry.chrome.js';
import * as sidebarRegistry from './registry.sidebar.js';

// Top-level multi-bundle registry. Each bundle key matches a rollup
// entry name in vite.config.mjs (app / chrome / sidebar) and carries its
// own route -> component map. The SSR sidecar selects the right bundle
// using the "bundle" field of the render request, then resolves the
// route key inside that bundle.
const BUNDLES = {
  app: appRegistry,
  chrome: chromeRegistry,
  sidebar: sidebarRegistry,
};

export function getRouteComponentForBundle(bundle, key) {
  const registry = BUNDLES[bundle];
  if (!registry) {
    throw new Error(`Unknown bundle: ${bundle}`);
  }
  return registry.getRouteComponent(key);
}

// Legacy convenience export so the client app-bundle entry that imports
// the bare app registry keeps working without an additional adapter.
export function getRouteComponent(key) {
  return appRegistry.getRouteComponent(key);
}
