# Solid migration: chrome + sidebar + permissions

Migrates three of the desktop client's Jinja templates to Solid SSR
bundles:

- `chrome.html` + `chrome.js` (the persistent titlebar / sidebar / iframe
  shell served at `/_chrome`) now render through a new `chrome` Vite
  rollup entry. The component tree lives under
  `frontend/src/components/chrome/{Titlebar,Sidebar,RequestsPanel,ProvidersPanel}.jsx`
  and the route entry is `frontend/src/routes/chrome.jsx`.
- `sidebar.html` + `sidebar.js` (the standalone sidebar
  `WebContentsView` served at `/_chrome/sidebar`) now render through a
  new `sidebar` Vite rollup entry that reuses the shared `Sidebar`
  component.
- `permissions.html`, `latchkey_predefined_permission.html`, and
  `latchkey_file_sharing_permission.html` (the modal permission dialog
  served at `/requests/<id>`) now render through new Solid routes under
  `frontend/src/routes/permissions/{index,predefined,file_sharing}.jsx`
  layered on the shared
  `frontend/src/components/permissions/PermissionRequest.jsx` shell.

The SSR sidecar's `/__ssr/render` endpoint now accepts a `bundle` field
selecting which client bundle's route registry to use; the Python
`SsrSidecar.render` shim and the `_render_ssr_or_fallback` helper carry
the same `bundle` parameter through so the fallback client-render shell
loads the matching entry script. The `_chrome` / `_chrome/sidebar`
routes and the latchkey permission renderers all dispatch through the
sidecar in production and fall back to the client-render shell when the
sidecar is unhealthy. Existing Jinja templates and static JS files are
left in place pending the Phase 8 cleanup pass.
