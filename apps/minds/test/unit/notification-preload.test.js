'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { runPreload } = require('./preload-context');

test('native notification clicks wait for the renderer action and are delivered once', () => {
  const ipc = new EventEmitter();
  const sent = [];
  ipc.send = (...args) => sent.push(args);
  const bridge = runPreload({ ipc }).mindsNative;
  const first = { id: 'msg-first' };
  const second = { id: 'msg-second' };
  ipc.emit('open-notification', {}, first);
  const opened = [];
  bridge.onOpenNotification((entry) => opened.push(entry));
  ipc.emit('open-notification', {}, second);
  assert.deepEqual(opened, [first, second]);
  assert.deepEqual(sent, [['notification-listener-ready']]);
});
