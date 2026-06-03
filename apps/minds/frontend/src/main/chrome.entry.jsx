import { hydrate } from 'solid-js/web';
import { getRouteComponent } from '../routes/registry.chrome.js';
import '../styles/globals.css';

// Client-side hydration entry for the "chrome" bundle (titlebar +
// sidebar + content shell). Loaded by FastAPI at /_chrome and rendered
// into the chrome WebContentsView in Electron. Same hydration contract
// as app.entry.jsx -- reads {route, props} from __route__ and hydrates.

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
