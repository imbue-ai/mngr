import { getRouteComponent } from '../routes/registry.js';
import { hydrateRouteFromBoot } from '../lib/hydrate_entry.jsx';
import '../styles/globals.css';

// Client-side hydration entry for the "app" bundle. The SSR HTML
// embeds the route key and initial props in a
// <script type="application/json" id="__route__"> blob; the shared
// hydrateRouteFromBoot helper reads it on boot and hydrates the
// resolved component into #app.
//
// In the fallback path (sidecar unhealthy, no SSR HTML), Solid's
// `hydrate` still works because the page contains an empty mount
// point -- the missing children just mean we client-render the whole
// thing.
hydrateRouteFromBoot(getRouteComponent);
