// Minimal Node SSR HTTP server. Receives render requests from the
// Python FastAPI front door (see ssr_sidecar.py) and returns an HTML
// document with the Solid component pre-rendered and a hydration payload
// embedded for the client bundle.
//
// Endpoints:
//   GET  /__ssr/health      -> 200 {"status":"ok"} when the server is up
//   POST /__ssr/render      body: {route, props}; returns rendered HTML
//
// Listening port:
//   process.env.MINDS_SSR_PORT (required; the Python supervisor picks a
//   free port and passes it in via env).
//
// The Vite client manifest is read from the path in MINDS_VITE_MANIFEST
// (relative paths land under the client build's outDir). When unset --
// typical in dev when the bundle hasn't been built yet -- we emit
// /src/main/app.entry.jsx as the script src so Vite's dev server serves
// the source directly.

import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { renderToStringAsync, generateHydrationScript } from 'solid-js/web';
import { getRouteComponent } from '../routes/registry.js';

const PORT = Number(process.env.MINDS_SSR_PORT || 0);
if (!PORT) {
  console.error('MINDS_SSR_PORT is required');
  process.exit(2);
}

const MANIFEST_PATH = process.env.MINDS_VITE_MANIFEST || null;
const VITE_DEV_URL = process.env.MINDS_VITE_DEV_URL || null;

let cachedManifest = null;
async function getManifest() {
  if (cachedManifest) return cachedManifest;
  if (!MANIFEST_PATH) return null;
  try {
    const text = await readFile(MANIFEST_PATH, 'utf-8');
    cachedManifest = JSON.parse(text);
    return cachedManifest;
  } catch (err) {
    console.warn(`SSR sidecar: failed to read manifest at ${MANIFEST_PATH}: ${err.message}`);
    return null;
  }
}

function resolveAssetTags(manifest) {
  // Returns { scriptTag, linkTags } for the `app` entry. The convention
  // matches Vite's manifest format -- look up the entry by its source
  // path, read `file` for the hashed bundle name, and emit any `css`
  // entries as preloaded stylesheets.
  if (!manifest) {
    const devSrc = VITE_DEV_URL
      ? `${VITE_DEV_URL}/src/main/app.entry.jsx`
      : '/_static/src/main/app.entry.jsx';
    const devCss = VITE_DEV_URL ? `${VITE_DEV_URL}/src/styles/globals.css` : null;
    return {
      scriptTag: `<script type="module" src="${devSrc}"></script>`,
      linkTags: devCss ? `<link rel="stylesheet" href="${devCss}">` : '',
    };
  }
  const entry = manifest['src/main/app.entry.jsx'];
  if (!entry) {
    throw new Error('Vite manifest missing src/main/app.entry.jsx entry');
  }
  const scriptTag = `<script type="module" src="/_static/_dist/${entry.file}"></script>`;
  const cssFiles = entry.css || [];
  const linkTags = cssFiles
    .map((href) => `<link rel="stylesheet" href="/_static/_dist/${href}">`)
    .join('');
  return { scriptTag, linkTags };
}

function escapeJsonForScript(value) {
  // Embedded JSON must not contain </script> sequences or U+2028/U+2029.
  // The unicode escapes in the regex bodies are required: the literal
  // U+2028 / U+2029 characters terminate a regex on the Babel parser
  // path Vite's Solid plugin uses, so we spell them out explicitly.
  return JSON.stringify(value)
    .replace(/</g, '\\u003c')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

async function renderRoute({ route, props }) {
  const Component = getRouteComponent(route);
  const body = await renderToStringAsync(() => <Component {...props} />);
  const hydrationScript = generateHydrationScript();
  const manifest = await getManifest();
  const { scriptTag, linkTags } = resolveAssetTags(manifest);
  const payload = escapeJsonForScript({ route, props });

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Minds</title>
${linkTags}
${hydrationScript}
</head>
<body class="bg-zinc-50 text-zinc-900 font-sans antialiased">
<div id="app">${body}</div>
<script type="application/json" id="__route__">${payload}</script>
${scriptTag}
</body>
</html>`;
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (chunk) => {
      data += chunk;
      // Cap so a runaway proxy can't OOM the sidecar. Initial-state
      // payloads stay well under this.
      if (data.length > 1_000_000) {
        reject(new Error('Request body too large'));
        req.destroy();
      }
    });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

const server = createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/__ssr/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok' }));
      return;
    }
    if (req.method === 'POST' && req.url === '/__ssr/render') {
      const text = await readBody(req);
      let request;
      try {
        request = JSON.parse(text || '{}');
      } catch {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Invalid JSON' }));
        return;
      }
      const route = typeof request.route === 'string' ? request.route : null;
      const props = request.props || {};
      if (!route) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Missing route' }));
        return;
      }
      try {
        const html = await renderRoute({ route, props });
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(html);
        return;
      } catch (err) {
        console.error('SSR render failed:', err);
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: String(err && err.message ? err.message : err) }));
        return;
      }
    }
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'Not found' }));
  } catch (err) {
    console.error('SSR sidecar error:', err);
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: String(err && err.message ? err.message : err) }));
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`SSR sidecar listening on 127.0.0.1:${PORT}`);
});

function shutdown() {
  server.close(() => process.exit(0));
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
