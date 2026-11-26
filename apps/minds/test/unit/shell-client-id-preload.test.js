'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createLocalStorage, runPreload } = require('./preload-context');

function recordingIpc(answer) {
  const ipc = new EventEmitter();
  ipc.syncAsks = [];
  ipc.sendSync = (...args) => {
    ipc.syncAsks.push(args);
    return answer;
  };
  ipc.send = () => assert.fail('a frame sent an async message');
  return ipc;
}

test('a frame the page frames directly gets no bridge, and runs as the client id main answers', () => {
  const ipc = recordingIpc('main-id');
  const localStorage = createLocalStorage({ 'si-client-id': 'partition-id' });
  const exposed = runPreload({ ipc, frame: 'child', localStorage });
  assert.deepEqual(exposed, {});
  assert.deepEqual(ipc.syncAsks, [['shell-client-id', 'partition-id']]);
  assert.equal(localStorage.getItem('si-client-id'), 'main-id');
  assert.equal(ipc.listenerCount('open-notification'), 0);
});

test('a frame main answers null for keeps its own id', () => {
  const ipc = recordingIpc(null);
  const localStorage = createLocalStorage({ 'si-client-id': 'app-id' });
  runPreload({ ipc, frame: 'child', localStorage });
  assert.equal(localStorage.getItem('si-client-id'), 'app-id');
});

test('a frame nested inside a workspace page asks main nothing and gets no bridge', () => {
  const ipc = recordingIpc('main-id');
  const localStorage = createLocalStorage();
  const exposed = runPreload({ ipc, frame: 'nested', localStorage });
  assert.deepEqual(exposed, {});
  assert.deepEqual(ipc.syncAsks, []);
  assert.equal(localStorage.getItem('si-client-id'), null);
});

test('the window\'s own page gets the bridge and asks for no client id', () => {
  const ipc = recordingIpc('main-id');
  const exposed = runPreload({ ipc, frame: 'main' });
  assert.deepEqual(Object.keys(exposed), ['mindsNative']);
  assert.deepEqual(ipc.syncAsks, []);
});
