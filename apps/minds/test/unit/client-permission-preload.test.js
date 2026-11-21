'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('the preload relays client-permission prompts to the page and its answers to main', () => {
  const ipc = new EventEmitter();
  const sent = [];
  ipc.send = (...args) => sent.push(args);
  let bridge;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../electron/preload.js'), 'utf8'), {
    process: { platform: 'darwin', argv: [] },
    require: () => ({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_name, surface) => { bridge = surface; } } }),
  });
  const prompts = [];
  bridge.onClientPermissionPrompt((prompt) => prompts.push(prompt));
  const prompt = { promptId: 'p1', workspaceId: 'agent-0a1b', permission: 'microphone', workspaceName: 'Research notes' };
  ipc.emit('client-permission-prompt', { sender: 'main' }, prompt);
  assert.deepEqual(prompts, [prompt]);

  const answer = { promptId: 'p1', decision: 'granted', isRemembered: false };
  bridge.answerClientPermission(answer);
  assert.deepEqual(sent, [['client-permission-answer', answer]]);
});

test('the preload relays a toast with its link to a workspace\'s Client permissions, and no link it does not recognize', () => {
  const ipc = new EventEmitter();
  let bridge;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../electron/preload.js'), 'utf8'), {
    process: { platform: 'darwin', argv: [] },
    require: () => ({ ipcRenderer: ipc, contextBridge: { exposeInMainWorld: (_name, surface) => { bridge = surface; } } }),
  });
  const toasts = [];
  bridge.onToast((toast) => toasts.push(toast));
  const title = 'Microphone blocked for "Research notes"';
  const body = 'Blocked on this computer. Change it under Permissions > Client permissions.';
  const link = { kind: 'client-permissions', workspaceId: 'agent-0a1b', text: 'Permissions > Client permissions' };
  ipc.emit('show-toast', { sender: 'main' }, { title, body, link });
  ipc.emit('show-toast', { sender: 'main' }, { title: "Couldn't open link", body: 'Copied instead.' });
  ipc.emit('show-toast', { sender: 'main' }, { title, body, link: { ...link, kind: 'settings' } });
  ipc.emit('show-toast', { sender: 'main' }, { title, body, link: { ...link, workspaceId: 7 } });
  // The preload builds each toast in the vm's realm, whose Object prototype is not this one's.
  assert.deepEqual(JSON.parse(JSON.stringify(toasts)), [
    { title, body, link },
    { title: "Couldn't open link", body: 'Copied instead.', link: null },
    { title, body, link: null },
    { title, body, link: null },
  ]);
});
