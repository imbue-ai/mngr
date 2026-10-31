// Unit tests for the main process's crashpad annotations.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  LAST_EVENT_KEY,
  MEMORY_KEY,
  NAV_KEY,
  classifyNavigationTarget,
  createCrashAnnotator,
  summarizeMemory,
} = require('../../electron/crash-annotations');

function fakeReporter() {
  const params = new Map();
  return { params, addExtraParameter: (key, value) => params.set(key, value) };
}

test('classifyNavigationTarget names the class of each hop of the workspace load', () => {
  assert.equal(classifyNavigationTarget('http://localhost:8420/workspace/agent-0123abcd'), 'backend');
  assert.equal(classifyNavigationTarget('http://127.0.0.1:8420/'), 'backend');
  assert.equal(classifyNavigationTarget('http://localhost:8420/forward-bridge?x=1'), 'bridge');
  assert.equal(classifyNavigationTarget('http://localhost:8421/_bridge'), 'bridge');
  assert.equal(classifyNavigationTarget('http://localhost:8421/goto/host-0123abcd/'), 'bridge');
  assert.equal(
    classifyNavigationTarget('https://system_interface-ab12.agent-0123abcd.localhost:8421/'),
    'workspace'
  );
  assert.equal(classifyNavigationTarget('https://host-0123abcd.localhost:8421/chat'), 'workspace');
  assert.equal(classifyNavigationTarget('file:///Applications/App.app/Contents/shell.html'), 'internal');
  assert.equal(classifyNavigationTarget('about:blank'), 'internal');
  assert.equal(classifyNavigationTarget('https://example.com/'), 'external');
  assert.equal(classifyNavigationTarget('not a url'), 'invalid');
  assert.equal(classifyNavigationTarget(undefined), 'invalid');
});

test('recordNavigation writes the milestone with the target class, never the URL', () => {
  const reporter = fakeReporter();
  const annotator = createCrashAnnotator(reporter, { uptimeMs: () => 4200 });
  annotator.recordNavigation({
    windowId: 3,
    isMainFrame: false,
    phase: 'start',
    url: 'https://system_interface-ab12.agent-0123abcd.localhost:8421/?token=secret',
    isSameDocument: false,
  });
  const value = reporter.params.get(NAV_KEY);
  assert.equal(value, 'window=3 frame=sub phase=start target=workspace same_doc=false uptime_ms=4200');
  assert.ok(!value.includes('agent-0123abcd'));
});

test('a later milestone overwrites the earlier one', () => {
  const reporter = fakeReporter();
  const annotator = createCrashAnnotator(reporter, { uptimeMs: () => 1 });
  annotator.recordNavigation({ windowId: 1, isMainFrame: true, phase: 'start', url: 'http://localhost:1/', isSameDocument: false });
  annotator.recordNavigation({ windowId: 1, isMainFrame: true, phase: 'commit', url: 'http://localhost:1/', isSameDocument: true });
  assert.equal(reporter.params.get(NAV_KEY), 'window=1 frame=main phase=commit target=backend same_doc=true uptime_ms=1');
});

test('an unexpected phase is recorded as unknown', () => {
  const reporter = fakeReporter();
  createCrashAnnotator(reporter, { uptimeMs: () => 1 }).recordNavigation({
    windowId: 1, isMainFrame: true, phase: 'bogus', url: 'about:blank', isSameDocument: false,
  });
  assert.match(reporter.params.get(NAV_KEY), /phase=unknown/);
});

test('summarizeMemory converts KB to MB and picks out the browser process', () => {
  const metrics = [
    { type: 'Browser', memory: { workingSetSize: 358400 } },
    { type: 'Tab', memory: { workingSetSize: 204800 } },
    { type: 'GPU', memory: { workingSetSize: 102400 } },
  ];
  assert.deepEqual(summarizeMemory(metrics, { free: 112640 }), {
    browserWorkingSetMb: 350,
    totalWorkingSetMb: 650,
    processCount: 3,
    systemFreeMb: 110,
  });
  assert.deepEqual(summarizeMemory([], null), {
    browserWorkingSetMb: null,
    totalWorkingSetMb: 0,
    processCount: 0,
    systemFreeMb: null,
  });
});

test('recordMemory and recordMainEvent write their own keys', () => {
  const reporter = fakeReporter();
  const annotator = createCrashAnnotator(reporter, { uptimeMs: () => 60000 });
  annotator.recordMemory([{ type: 'Browser', memory: { workingSetSize: 1024 } }], null);
  annotator.recordMainEvent('notification-show');
  assert.equal(reporter.params.get(MEMORY_KEY), 'browser_ws_mb=1 total_ws_mb=1 processes=1 system_free_mb=unknown uptime_ms=60000');
  assert.equal(reporter.params.get(LAST_EVENT_KEY), 'event=notification-show uptime_ms=60000');
});

test('every key fits crashpad\'s 39-byte key limit', () => {
  for (const key of [NAV_KEY, MEMORY_KEY, LAST_EVENT_KEY]) {
    assert.ok(Buffer.byteLength(key) <= 39, key);
  }
});

test('a null reporter makes every method a no-op', () => {
  let uptimeReads = 0;
  const annotator = createCrashAnnotator(null, { uptimeMs: () => { uptimeReads += 1; return 1; } });
  annotator.recordMainEvent('window-focus');
  annotator.recordMemory([], null);
  annotator.recordNavigation({ windowId: 1, isMainFrame: true, phase: 'start', url: 'about:blank', isSameDocument: false });
  assert.equal(uptimeReads, 0);
});

test('a throwing reporter warns once and never throws', () => {
  const warnings = [];
  const reporter = { addExtraParameter: () => { throw new Error('crashReporter not started'); } };
  const annotator = createCrashAnnotator(reporter, { uptimeMs: () => 1, warn: (m) => warnings.push(m) });
  annotator.recordMainEvent('window-focus');
  annotator.recordMainEvent('window-blur');
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /crashReporter not started/);
});
