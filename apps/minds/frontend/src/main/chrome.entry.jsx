import { getRouteComponent } from '../routes/registry.chrome.js';
import { hydrateRouteFromBoot } from '../lib/hydrate_entry.jsx';
import '../styles/globals.css';

// Client-side hydration entry for the "chrome" bundle (titlebar +
// sidebar + content shell). Loaded by FastAPI at /_chrome and rendered
// into the chrome WebContentsView in Electron. Shares the boot
// contract with the app and sidebar bundles via hydrateRouteFromBoot:
// each entry hands the helper its own per-bundle route resolver.
hydrateRouteFromBoot(getRouteComponent);
