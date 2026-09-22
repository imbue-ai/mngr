'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('native notification clicks wait for the renderer action and are delivered once', () => {
  const ipc = new EventEmitter();
  const sent = [];
  ipc.send = (...args) => sent.push(args);
  let bridge;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../electron/preload.js'), 'utf8'), {
    process: { platform: 'darwin' },
    require: () => ({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_name, surface) => { bridge = surface; } } }),
  });
  const first = { id: 'msg-first' };
  const second = { id: 'msg-second' };
  ipc.emit('open-notification', {}, first);
  const opened = [];
  bridge.onOpenNotification((entry) => opened.push(entry));
  ipc.emit('open-notification', {}, second);
  assert.deepEqual(opened, [first, second]);
  assert.deepEqual(sent, [['notification-listener-ready']]);
});
