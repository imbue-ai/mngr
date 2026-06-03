import { SidebarRoute } from './sidebar.jsx';

// Route registry for the "sidebar" bundle (standalone sidebar
// WebContentsView, served at /_chrome/sidebar).
export const ROUTES = {
  sidebar: SidebarRoute,
};

export function getRouteComponent(key) {
  const Component = ROUTES[key];
  if (!Component) {
    throw new Error(`Unknown sidebar route key: ${key}`);
  }
  return Component;
}
