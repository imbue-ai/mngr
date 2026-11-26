'use strict';

// Runs electron/preload.js under plain node, as the given frame of a window:
// `main` (the Imbue Studio page), `child` (a frame the page frames directly)
// or `nested` (a frame inside a child). `ipc` stands in for ipcRenderer and
// `exposed` collects what the preload hands contextBridge, by name.

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const PRELOAD_SOURCE = fs.readFileSync(path.join(__dirname, '../../electron/preload.js'), 'utf8');

function createLocalStorage(entries = {}) {
  const items = new Map(Object.entries(entries));
  return {
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
  };
}

function runPreload({ ipc, frame = 'main', localStorage = createLocalStorage() }) {
  const exposed = {};
  const top = {};
  const window = frame === 'main' ? top : { top, parent: frame === 'child' ? top : {}, localStorage };
  if (frame === 'main') top.top = top;
  vm.runInNewContext(PRELOAD_SOURCE, {
    window,
    process: { platform: 'darwin', argv: [] },
    require: () => ({
      ipcRenderer: ipc,
      contextBridge: { exposeInMainWorld: (name, surface) => { exposed[name] = surface; } },
    }),
  });
  return exposed;
}

module.exports = { createLocalStorage, runPreload };
