'use strict';

// Unit tests for the client-permission gate (electron/client-permission-policy.js).
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  DECISIONS,
  canonicalWorkspaceIdOf,
  clientPermissionNameOf,
  createClientPermissionGate,
  createDecisionCache,
  createSessionGrants,
  decisionKey,
  describeDenialToast,
  isClientPermissionAnswer,
  isRequestOfMountedWorkspace,
  parseDecisionBody,
  workspaceIdOfRequestingUrl,
} = require('../../electron/client-permission-policy');

const AGENT = 'agent-0a1b2c3d4e5f';
const HOST = 'host-99887766aabb';
const OTHER_AGENT = 'agent-77aa88bb99cc';
const MICROPHONE = 'microphone';

function frameUrl(workspaceId, app = 'chat') {
  return `http://${app}.${workspaceId}.localhost:8421/`;
}

function mountOfAgent(workspaceId = AGENT, aliases = [workspaceId, HOST]) {
  return { workspaceId, workspaceIds: aliases, opensLinks: true, opensCustomSchemeLinks: false };
}

test('clientPermissionNameOf maps a media request or check for audio to the microphone', () => {
  assert.equal(clientPermissionNameOf('media', { mediaTypes: ['audio'] }), MICROPHONE);
  assert.equal(clientPermissionNameOf('media', { mediaTypes: ['video', 'audio'] }), MICROPHONE);
  assert.equal(clientPermissionNameOf('media', { mediaType: 'audio' }), MICROPHONE);
});

test('clientPermissionNameOf maps nothing else to a client permission', () => {
  assert.equal(clientPermissionNameOf('media', { mediaTypes: ['video'] }), null);
  assert.equal(clientPermissionNameOf('media', { mediaTypes: [] }), null);
  assert.equal(clientPermissionNameOf('media', { mediaType: 'video' }), null);
  assert.equal(clientPermissionNameOf('media', { mediaType: 'unknown' }), null);
  assert.equal(clientPermissionNameOf('media', {}), null);
  assert.equal(clientPermissionNameOf('media', null), null);
  assert.equal(clientPermissionNameOf('media', undefined), null);
  assert.equal(clientPermissionNameOf('media', { mediaTypes: 'audio' }), null);
  assert.equal(clientPermissionNameOf('notifications', { mediaTypes: ['audio'] }), null);
  assert.equal(clientPermissionNameOf('fullscreen', { mediaType: 'audio' }), null);
});

test('workspaceIdOfRequestingUrl names the workspace of both id forms, lowercased', () => {
  assert.equal(workspaceIdOfRequestingUrl(frameUrl(AGENT)), AGENT);
  assert.equal(workspaceIdOfRequestingUrl(`http://${AGENT}.localhost:8421/chat`), AGENT);
  assert.equal(workspaceIdOfRequestingUrl(frameUrl(HOST, 'terminal')), HOST);
  assert.equal(workspaceIdOfRequestingUrl(frameUrl(AGENT.toUpperCase())), AGENT);
});

test('workspaceIdOfRequestingUrl is null for the chrome, other origins, and no URL', () => {
  for (const url of [
    'http://localhost:8421/',
    'http://127.0.0.1:8421/',
    'https://example.com/',
    'file:///Applications/loading.html',
    'about:blank',
    `http://${AGENT}.example.com/`,
    '',
    null,
    undefined,
  ]) {
    assert.equal(workspaceIdOfRequestingUrl(url), null, String(url));
  }
});

test('isRequestOfMountedWorkspace answers whether the id is one of the mount aliases, under any case', () => {
  const mount = mountOfAgent();
  assert.equal(isRequestOfMountedWorkspace(AGENT, mount), true);
  assert.equal(isRequestOfMountedWorkspace(HOST, mount), true);
  assert.equal(isRequestOfMountedWorkspace(HOST, mountOfAgent(AGENT, [AGENT, HOST.toUpperCase()])), true);
  assert.equal(isRequestOfMountedWorkspace(OTHER_AGENT, mount), false);
  assert.equal(isRequestOfMountedWorkspace(null, mount), false);
  assert.equal(isRequestOfMountedWorkspace(AGENT, null), false);
});

test('canonicalWorkspaceIdOf prefers the agent alias, the only form the backend accepts', () => {
  assert.equal(canonicalWorkspaceIdOf(mountOfAgent(AGENT, [AGENT, HOST])), AGENT);
  assert.equal(canonicalWorkspaceIdOf({ workspaceId: HOST, workspaceIds: [HOST, AGENT] }), AGENT);
  assert.equal(canonicalWorkspaceIdOf({ workspaceId: HOST, workspaceIds: [HOST] }), HOST);
});

test('decisionKey separates workspaces and permissions', () => {
  assert.equal(decisionKey(AGENT, MICROPHONE), decisionKey(AGENT, MICROPHONE));
  assert.notEqual(decisionKey(AGENT, MICROPHONE), decisionKey(OTHER_AGENT, MICROPHONE));
  assert.notEqual(decisionKey(AGENT, MICROPHONE), decisionKey(AGENT, 'camera'));
});

test('the decision cache answers until its TTL, and forgets on delete and clear', () => {
  let clock = 1000;
  const cache = createDecisionCache({ ttlMs: 5000, now: () => clock });
  assert.equal(cache.get('a'), undefined);
  cache.set('a', 'granted');
  cache.set('b', 'denied');
  clock += 4999;
  assert.equal(cache.get('a'), 'granted');
  clock += 1;
  assert.equal(cache.get('a'), undefined);
  cache.set('a', 'ask');
  assert.equal(cache.get('a'), 'ask');
  cache.delete('a');
  assert.equal(cache.get('a'), undefined);
  cache.set('a', 'ask');
  cache.set('c', 'ask');
  cache.clear();
  assert.equal(cache.get('a'), undefined);
  assert.equal(cache.get('c'), undefined);
});

test('session grants hold the keys added to them', () => {
  const grants = createSessionGrants();
  assert.equal(grants.has('a'), false);
  grants.add('a');
  assert.equal(grants.has('a'), true);
  assert.equal(grants.has('b'), false);
});

test('parseDecisionBody reads the route answer, and nothing that is not one', () => {
  assert.deepEqual(parseDecisionBody('{"decision":"ask","source":"default","workspace_name":"Research notes"}'), {
    decision: 'ask',
    source: 'default',
    workspaceName: 'Research notes',
  });
  assert.deepEqual(parseDecisionBody('{"decision":"denied","source":"workspace"}'), {
    decision: 'denied',
    source: 'workspace',
    workspaceName: null,
  });
  for (const body of [
    '',
    'not json',
    'null',
    '"granted"',
    '{}',
    '{"decision":"maybe","source":"workspace"}',
    '{"decision":"granted","source":"install"}',
    '{"decision":"granted"}',
  ]) {
    assert.equal(parseDecisionBody(body), null, body);
  }
});

test('describeDenialToast names the workspace and where to change the answer', () => {
  const toast = describeDenialToast('Research notes');
  assert.deepEqual(toast, {
    title: 'Microphone blocked for "Research notes"',
    body: "This workspace isn't allowed to use the microphone on this computer. Change this under Permissions, Client permissions.",
  });
  assert.doesNotMatch(toast.title + toast.body, /[–—]/);
});

test('isClientPermissionAnswer accepts only the declared answer shape', () => {
  assert.equal(isClientPermissionAnswer({ promptId: 'p1', decision: 'granted', isRemembered: true }), true);
  assert.equal(isClientPermissionAnswer({ promptId: 'p1', decision: 'denied', isRemembered: false }), true);
  for (const payload of [
    null,
    undefined,
    'granted',
    {},
    { promptId: '', decision: 'granted', isRemembered: true },
    { promptId: 7, decision: 'granted', isRemembered: true },
    { promptId: 'p1', decision: 'ask', isRemembered: true },
    { promptId: 'p1', decision: 'granted' },
    { promptId: 'p1', decision: 'granted', isRemembered: 'yes' },
  ]) {
    assert.equal(isClientPermissionAnswer(payload), false, JSON.stringify(payload));
  }
});

// A gate over fakes: windows are plain objects, mounts are set per window,
// the backend answers from `stored` (null for "could not be had"), and the
// sends, toasts, warnings and timers are recorded.
function makeGate({ stored = { decision: 'ask', source: 'default', workspaceName: 'Research notes' } } = {}) {
  const harness = {
    clock: 0,
    mounts: new Map(),
    stored,
    fetches: [],
    prompts: [],
    toasts: [],
    warnings: [],
    timers: [],
    isPageGone: false,
  };
  let nextPromptId = 0;
  harness.gate = createClientPermissionGate({
    mountOf: (webContents) => harness.mounts.get(webContents) || null,
    mountShowing: (workspaceId) => {
      for (const mount of harness.mounts.values()) {
        if (isRequestOfMountedWorkspace(workspaceId, mount)) return mount;
      }
      return null;
    },
    fetchDecision: async (workspaceId, permission) => {
      harness.fetches.push({ workspaceId, permission });
      return typeof harness.stored === 'function' ? harness.stored() : harness.stored;
    },
    sendPrompt: (webContents, prompt) => {
      if (harness.isPageGone) return false;
      harness.prompts.push({ webContents, ...prompt });
      return true;
    },
    showToast: (webContents, toast) => harness.toasts.push({ webContents, ...toast }),
    newPromptId: () => `prompt-${++nextPromptId}`,
    warn: (message) => harness.warnings.push(message),
    cacheTtlMs: 5000,
    promptTimeoutMs: 600000,
    now: () => harness.clock,
    schedule: (fn, ms) => {
      const timer = { fn, ms, isCancelled: false };
      harness.timers.push(timer);
      return timer;
    },
    cancel: (timer) => {
      timer.isCancelled = true;
    },
  });
  return harness;
}

// Issue a request and collect its answers (an array, so a second answer to
// one callback would show).
async function request(harness, webContents, url = frameUrl(AGENT)) {
  const answers = [];
  await harness.gate.request(webContents, MICROPHONE, url, (isGranted) => answers.push(isGranted));
  return answers;
}

test('a request from a window with no mount, or from a frame of another workspace, is denied without asking the backend', async () => {
  const harness = makeGate({ stored: { decision: 'granted', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  assert.deepEqual(await request(harness, win), [false]);
  harness.mounts.set(win, mountOfAgent());
  assert.deepEqual(await request(harness, win, frameUrl(OTHER_AGENT)), [false]);
  assert.deepEqual(await request(harness, win, 'http://localhost:8421/'), [false]);
  assert.deepEqual(harness.fetches, []);
  assert.deepEqual(harness.toasts, []);
});

test('a stored granted grants, under the canonical agent id even from a legacy host-keyed frame', async () => {
  const harness = makeGate({ stored: { decision: 'granted', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  assert.deepEqual(await request(harness, win, frameUrl(HOST)), [true]);
  assert.deepEqual(harness.fetches, [{ workspaceId: AGENT, permission: MICROPHONE }]);
  assert.deepEqual(harness.prompts, []);
});

test('a stored denial denies and toasts once per workspace per run, naming the workspace', async () => {
  const harness = makeGate({ stored: { decision: 'denied', source: 'workspace', workspaceName: 'Research notes' } });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  assert.deepEqual(await request(harness, win), [false]);
  harness.clock += 60000;
  assert.deepEqual(await request(harness, win), [false]);
  assert.equal(harness.toasts.length, 1);
  assert.deepEqual(harness.toasts[0], { webContents: win, ...describeDenialToast('Research notes') });
});

test('a default denial toasts too, and each workspace gets its own toast', async () => {
  const harness = makeGate({ stored: { decision: 'denied', source: 'default', workspaceName: 'Research notes' } });
  const win = {};
  const other = {};
  harness.mounts.set(win, mountOfAgent());
  harness.mounts.set(other, mountOfAgent(OTHER_AGENT, [OTHER_AGENT]));
  await request(harness, win);
  await request(harness, other, frameUrl(OTHER_AGENT));
  assert.deepEqual(harness.toasts.map((toast) => toast.webContents), [win, other]);
});

test('a backend that cannot answer denies, uncached and without a toast', async () => {
  const harness = makeGate({ stored: null });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  assert.deepEqual(await request(harness, win), [false]);
  assert.deepEqual(await request(harness, win), [false]);
  assert.equal(harness.fetches.length, 2);
  assert.deepEqual(harness.toasts, []);
});

test('a backend call that throws denies with a warning', async () => {
  const harness = makeGate({
    stored: () => {
      throw new Error('boom');
    },
  });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  assert.deepEqual(await request(harness, win), [false]);
  assert.equal(harness.warnings.length, 1);
  assert.match(harness.warnings[0], /boom/);
});

test('a good answer is cached for the TTL, then read again', async () => {
  const harness = makeGate({ stored: { decision: 'granted', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  await request(harness, win);
  harness.clock += 4000;
  await request(harness, win);
  assert.equal(harness.fetches.length, 1);
  harness.clock += 1000;
  await request(harness, win);
  assert.equal(harness.fetches.length, 2);
});

test('requests arriving while the backend is being asked share one call', async () => {
  const harness = makeGate({ stored: { decision: 'granted', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  const both = await Promise.all([request(harness, win), request(harness, win)]);
  assert.deepEqual(both, [[true], [true]]);
  assert.equal(harness.fetches.length, 1);
});

test('ask opens the prompt in the asking window and the answer settles the request', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  const answers = await request(harness, win);
  assert.deepEqual(answers, []);
  assert.deepEqual(harness.prompts, [
    { webContents: win, promptId: 'prompt-1', workspaceId: AGENT, permission: MICROPHONE, workspaceName: 'Research notes' },
  ]);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, win), true);
  assert.deepEqual(answers, [true]);
  assert.equal(harness.timers[0].isCancelled, true);
});

test('a second request while a prompt is open joins it, from any window showing the workspace', async () => {
  const harness = makeGate();
  const win = {};
  const second = {};
  harness.mounts.set(win, mountOfAgent());
  harness.mounts.set(second, mountOfAgent());
  const first = await request(harness, win);
  const joined = await request(harness, second, frameUrl(HOST));
  assert.equal(harness.prompts.length, 1);
  harness.gate.answer({ promptId: 'prompt-1', decision: 'denied', isRemembered: false }, win);
  assert.deepEqual(first, [false]);
  assert.deepEqual(joined, [false]);
  assert.deepEqual(harness.toasts, []);
});

test('an unremembered Allow is a session grant: later asks grant without a prompt', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  await request(harness, win);
  harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: false }, win);
  assert.deepEqual(await request(harness, win), [true]);
  harness.clock += 60000;
  assert.deepEqual(await request(harness, win), [true]);
  assert.equal(harness.prompts.length, 1);
  const verdict = await harness.gate.decide(win, MICROPHONE, frameUrl(AGENT));
  assert.equal(verdict.decision, DECISIONS.GRANTED);
});

test('a session grant never outranks a stored denial set later in the run', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  await request(harness, win);
  harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: false }, win);
  harness.stored = { decision: 'denied', source: 'workspace', workspaceName: 'Research notes' };
  harness.clock += 5000;
  assert.deepEqual(await request(harness, win), [false]);
  assert.equal(harness.toasts.length, 1);
});

test('an unremembered Don\'t allow is one-shot: the next request prompts again', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  await request(harness, win);
  harness.gate.answer({ promptId: 'prompt-1', decision: 'denied', isRemembered: false }, win);
  await request(harness, win);
  assert.equal(harness.prompts.length, 2);
});

test('a remembered answer drops the cached decision so the store is read again', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  await request(harness, win);
  harness.stored = { decision: 'granted', source: 'workspace', workspaceName: 'Research notes' };
  harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, win);
  assert.deepEqual(await request(harness, win), [true]);
  assert.equal(harness.fetches.length, 2);
});

test('answers that are malformed, to no open prompt, or from another window are dropped', async () => {
  const harness = makeGate();
  const win = {};
  const stranger = {};
  harness.mounts.set(win, mountOfAgent());
  const answers = await request(harness, win);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'ask', isRemembered: true }, win), false);
  assert.equal(harness.gate.answer({ promptId: 'prompt-9', decision: 'granted', isRemembered: true }, win), false);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, stranger), false);
  assert.deepEqual(answers, []);
  assert.equal(harness.warnings.length, 3);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, win), true);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, win), false);
  assert.deepEqual(answers, [true]);
});

test('a prompt whose window goes away denies every waiting request', async () => {
  const harness = makeGate();
  const win = {};
  const second = {};
  harness.mounts.set(win, mountOfAgent());
  harness.mounts.set(second, mountOfAgent());
  const first = await request(harness, win);
  const joined = await request(harness, second);
  harness.gate.forgetWebContents(second);
  assert.deepEqual(first, []);
  harness.gate.forgetWebContents(win);
  assert.deepEqual(first, [false]);
  assert.deepEqual(joined, [false]);
  assert.equal(harness.timers[0].isCancelled, true);
  assert.equal(harness.gate.answer({ promptId: 'prompt-1', decision: 'granted', isRemembered: true }, win), false);
});

test('a prompt whose window mounts another workspace, or none, denies; the same workspace keeps it', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  const answers = await request(harness, win);
  harness.mounts.set(win, mountOfAgent(AGENT, [AGENT, HOST]));
  harness.gate.mountChanged(win);
  assert.deepEqual(answers, []);
  harness.mounts.set(win, mountOfAgent(OTHER_AGENT, [OTHER_AGENT]));
  harness.gate.mountChanged(win);
  assert.deepEqual(answers, [false]);

  harness.mounts.set(win, mountOfAgent());
  const again = await request(harness, win);
  harness.mounts.delete(win);
  harness.gate.mountChanged(win);
  assert.deepEqual(again, [false]);
});

test('a prompt unanswered past its timeout denies every waiting request', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  const answers = await request(harness, win);
  assert.equal(harness.timers[0].ms, 600000);
  harness.timers[0].fn();
  assert.deepEqual(answers, [false]);
  await request(harness, win);
  assert.equal(harness.prompts.length, 2);
});

test('a prompt that cannot be sent denies at once', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  harness.isPageGone = true;
  assert.deepEqual(await request(harness, win), [false]);
  assert.deepEqual(harness.timers, []);
});

test('a window that leaves the workspace while the backend is asked is denied rather than prompted', async () => {
  const harness = makeGate();
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  harness.stored = () => {
    harness.mounts.delete(win);
    return { decision: 'ask', source: 'default', workspaceName: 'n' };
  };
  assert.deepEqual(await request(harness, win), [false]);
  assert.deepEqual(harness.prompts, []);
});

test('the check answers from the cache alone, warming it on a miss', async () => {
  const harness = makeGate({ stored: { decision: 'denied', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  harness.mounts.set(win, mountOfAgent());
  assert.equal(harness.gate.check(win, MICROPHONE, frameUrl(AGENT)), true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.fetches.length, 1);
  assert.equal(harness.gate.check(win, MICROPHONE, frameUrl(AGENT)), false);

  for (const decision of ['granted', 'ask']) {
    harness.clock += 5000;
    harness.stored = { decision, source: 'workspace', workspaceName: 'n' };
    harness.gate.check(win, MICROPHONE, frameUrl(AGENT));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(harness.gate.check(win, MICROPHONE, frameUrl(AGENT)), true, decision);
  }
});

test('the check is false off the mounted workspace, and finds the mount in any window when given none', async () => {
  const harness = makeGate({ stored: { decision: 'granted', source: 'workspace', workspaceName: 'n' } });
  const win = {};
  assert.equal(harness.gate.check(win, MICROPHONE, frameUrl(AGENT)), false);
  assert.equal(harness.gate.check(null, MICROPHONE, frameUrl(AGENT)), false);
  harness.mounts.set(win, mountOfAgent());
  assert.equal(harness.gate.check(win, MICROPHONE, frameUrl(OTHER_AGENT)), false);
  assert.equal(harness.gate.check(null, MICROPHONE, frameUrl(HOST)), true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(harness.fetches, [{ workspaceId: AGENT, permission: MICROPHONE }]);
});
