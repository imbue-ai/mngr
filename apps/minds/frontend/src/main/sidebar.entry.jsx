import { getRouteComponent } from '../routes/registry.sidebar.js';
import { hydrateRouteFromBoot } from '../lib/hydrate_entry.jsx';
import '../styles/globals.css';

// Client-side hydration entry for the "sidebar" bundle (standalone
// workspace list rendered into the Electron sidebar WebContentsView).
// Shares the boot contract with the app and chrome bundles via
// hydrateRouteFromBoot.
hydrateRouteFromBoot(getRouteComponent);
