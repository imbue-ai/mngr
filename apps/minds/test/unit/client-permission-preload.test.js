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
