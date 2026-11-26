// The one-time import of the shell client ids the default session held.
//
// CLEANUP: remove this module, legacy-shell-client-id-import.js, their call in
// main.js (importLegacyShellClientIds), legacyShellOrigins in
// shell-client-ids.js and the store's legacy-import flag once the beta and
// stable channels have carried this import (the first release after minds
// 0.10.7) for a release: every install that ran a window in the default
// session has launched a build that imports by then.
//
// Up to minds 0.10.5 every window ran in the default session, so each shell's
// `si-client-id` sits in the default session's localStorage. The account
// partitions (0.10.6) moved windows into `persist:account-<id>` sessions,
// whose storage starts empty, so the first launch on them registered every
// workspace's shell as a new client with all its windows minimized. This
// reads the old ids back so main's store can hand them to the shells.
//
// localStorage has no API that reads another session's entries, and the
// default session must not have its loads intercepted, so the default
// session's `Local Storage` directory is copied into a session of its own,
// whose requests are all answered here: a page on `http://localhost/` (the
// top-level site the Imbue Studio page framed each shell under, which is part
// of the key Chromium stores a framed origin's entries by) frames each
// candidate shell origin, and each frame reads its entry. The copy stays open
// for the rest of the process, so the next launch removes it.

const fs = require('fs');
const path = require('path');
const { BrowserWindow, session } = require('electron');
const { shellHostnameOf } = require('./shell-client-ids');
const { legacyImportCopyDir, removeLegacyImportCopy } = require('./legacy-shell-client-id-import');

const LOCAL_STORAGE_DIRNAME = 'Local Storage';
const TOP_PAGE_URL = 'http://localhost/';
const CLIENT_ID_STORAGE_KEY = 'si-client-id';

function topPageHtml(origins) {
  const frames = origins.map((origin) => `<iframe src="${encodeURI(`${origin}/`)}"></iframe>`).join('\n');
  return `<!doctype html><html><body>\n${frames}\n</body></html>`;
}

function withTimeout(promise, timeoutMs, what) {
  let timer;
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} took longer than ${timeoutMs}ms`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function readIdsFromFrames(win) {
  // Resolves on the page's load, which waits for every frame's.
  await win.loadURL(TOP_PAGE_URL);
  const idsByOrigin = new Map();
  for (const frame of win.webContents.mainFrame.frames) {
    const clientId = await frame.executeJavaScript(`localStorage.getItem(${JSON.stringify(CLIENT_ID_STORAGE_KEY)})`);
    if (typeof clientId === 'string' && clientId) idsByOrigin.set(new URL(frame.url).origin, clientId);
  }
  return idsByOrigin;
}

async function readFrames(stateDir, origins, timeoutMs) {
  const copyDir = legacyImportCopyDir(stateDir);
  removeLegacyImportCopy(stateDir);
  fs.mkdirSync(copyDir, { recursive: true });
  fs.cpSync(path.join(stateDir, LOCAL_STORAGE_DIRNAME), path.join(copyDir, LOCAL_STORAGE_DIRNAME), { recursive: true });

  const ses = session.fromPath(copyDir);
  const html = (body) => new Response(body, { headers: { 'content-type': 'text/html' } });
  ses.protocol.handle('http', () => html(topPageHtml(origins)));
  ses.protocol.handle('https', () => html('<!doctype html>'));
  const win = new BrowserWindow({
    show: false,
    webPreferences: { session: ses, contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  try {
    return await withTimeout(readIdsFromFrames(win), timeoutMs, 'reading the default session\'s shell client ids');
  } finally {
    win.destroy();
    ses.protocol.unhandle('http');
    ses.protocol.unhandle('https');
  }
}

/**
 * The `si-client-id` `origins` hold in the default session's localStorage, by
 * hostname, each hostname's from the first of its origins that has one;
 * empty, without reading, when there are no origins or the default session
 * has no localStorage at all (an install that never ran a window in it).
 *
 * @param {{ stateDir: string, origins: string[], timeoutMs: number }} options
 * @returns {Promise<Map<string, string>>}
 */
async function readLegacyShellClientIds({ stateDir, origins, timeoutMs }) {
  const idsByHostname = new Map();
  if (origins.length === 0 || !fs.existsSync(path.join(stateDir, LOCAL_STORAGE_DIRNAME))) return idsByHostname;
  const idsByOrigin = await readFrames(stateDir, origins, timeoutMs);
  for (const origin of origins) {
    const hostname = shellHostnameOf(origin);
    const clientId = idsByOrigin.get(new URL(origin).origin);
    if (hostname !== null && clientId !== undefined && !idsByHostname.has(hostname)) idsByHostname.set(hostname, clientId);
  }
  return idsByHostname;
}

module.exports = {
  readLegacyShellClientIds,
};
