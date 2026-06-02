import { defineConfig } from 'vite';
import solid from 'vite-plugin-solid';
import tailwind from '@tailwindcss/vite';
import { resolve } from 'node:path';

// Vite drives both the dev server (HMR proxied through FastAPI when
// MINDS_VITE_DEV_URL is set) and the production build that lands in
// apps/minds/imbue/minds/desktop_client/static/_dist/.
//
// Build modes:
//   * default (no --ssr): client bundles (multi-page, one entry per
//     Electron WebContentsView: app, chrome, sidebar). Emits manifest.json
//     so the Python SSR sidecar can resolve hashed asset paths.
//   * --ssr frontend/src/main/server.jsx: a Node bundle for the SSR HTTP
//     sidecar. Loaded by Python via the ssr_sidecar.py supervisor.
//
// Output convention:
//   client build -> apps/minds/imbue/minds/desktop_client/static/_dist/
//   ssr    build -> apps/minds/frontend/dist-server/
//
// The client build lands inside the Python package so FastAPI's existing
// StaticFiles mount serves it without extra plumbing.
export default defineConfig({
  root: resolve(__dirname, 'frontend'),
  plugins: [solid({ ssr: true }), tailwind()],
  build: {
    outDir: resolve(__dirname, 'imbue/minds/desktop_client/static/_dist'),
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        app: resolve(__dirname, 'frontend/src/main/app.entry.jsx'),
      },
      output: {
        // Stable filenames so still-Jinja templates can load the CSS by
        // name (``/_static/_dist/assets/app.css``) until the migration
        // consumes them; hashed JS bundles still cache-bust correctly via
        // the manifest the SSR sidecar reads.
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name].[ext]',
      },
    },
  },
  server: {
    port: Number(process.env.MINDS_VITE_DEV_PORT || 5173),
    strictPort: true,
    // The dev server is reachable directly (for HMR's websocket) and via
    // FastAPI's reverse proxy for normal asset GETs.
    host: '127.0.0.1',
  },
  resolve: {
    conditions: ['solid'],
  },
});
