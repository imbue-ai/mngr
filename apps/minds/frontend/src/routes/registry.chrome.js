import { ChromeRoute } from './chrome.jsx';

// Route registry for the "chrome" bundle (titlebar + sidebar + content
// iframe shell, served at /_chrome).
export const ROUTES = {
  chrome: ChromeRoute,
};

export function getRouteComponent(key) {
  const Component = ROUTES[key];
  if (!Component) {
    throw new Error(`Unknown chrome route key: ${key}`);
  }
  return Component;
}
