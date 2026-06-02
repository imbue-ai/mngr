import { hydrate } from 'solid-js/web';
import { getRouteComponent } from '../routes/registry.js';
import '../styles/globals.css';

// Client-side hydration entry. The SSR HTML embeds the route key and
// initial props in a <script type="application/json" id="__route__">
// blob; we read it on boot, look up the matching component, and hydrate
// into #app.
//
// In the fallback path (sidecar unhealthy, no SSR HTML), Solid's
// `hydrate` still works because the page contains an empty mount point
// -- the missing children just mean we client-render the whole thing.

function readBoot() {
  const node = document.getElementById('__route__');
  if (!node || !node.textContent) {
    throw new Error('Missing #__route__ JSON payload on page');
  }
  return JSON.parse(node.textContent);
}

function mount() {
  const { route, props } = readBoot();
  const Component = getRouteComponent(route);
  const target = document.getElementById('app');
  if (!target) {
    throw new Error('Missing #app mount target');
  }
  hydrate(() => <Component {...props} />, target);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mount, { once: true });
} else {
  mount();
}
