// Unit tests for the shell's half of the opt-in profiling mode: the launch
// switch, the categories Chromium records, and the flush of Chromium's trace
// to the backend at quit.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  PROFILE_SWITCH,
  PROFILE_FILE_ENV_VAR,
  CHROMIUM_TRACE_CATEGORIES,
  CHROMIUM_TRACE_BUFFER_KB,
  FLUSH_TIMEOUT_MS,
  profilePathFromLaunch,
  profileFileEnv,
  chromiumTracingOptions,
  sidecarPathFor,
  flushChromiumTrace,
} = require('../../electron/profiling');

test('the switch and the variable are the names the backend and the spec use', () => {
  assert.equal(PROFILE_SWITCH, '--profile');
  assert.equal(PROFILE_FILE_ENV_VAR, 'MINDS_PROFILE_FILE');
});

test('the launch argument names the file, as a separate value or joined with =', () => {
  assert.equal(
    profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile', '/tmp/share.json'], env: {} }),
    '/tmp/share.json',
  );
  assert.equal(
    profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile=/tmp/share.json', '--background'], env: {} }),
    '/tmp/share.json',
  );
  assert.equal(
    profilePathFromLaunch({ args: ['/app/Imbue Studio', '--background', '--profile', 'relative/share.json'], env: {} }),
    'relative/share.json',
  );
});

test('the argument wins over the variable, and the variable stands in when there is no argument', () => {
  const env = { MINDS_PROFILE_FILE: '/home/alice/profiles/dev.json' };
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile', '/tmp/arg.json'], env }), '/tmp/arg.json');
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio'], env }), '/home/alice/profiles/dev.json');
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--background'], env }), '/home/alice/profiles/dev.json');
});

test('no switch and no variable, or an empty value, is off', () => {
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio'], env: {} }), null);
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio'], env: { MINDS_PROFILE_FILE: '' } }), null);
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile='], env: {} }), null);
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile'], env: {} }), null);
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile', '--background'], env: {} }), null);
  assert.equal(profilePathFromLaunch({ args: undefined, env: undefined }), null);
});

test('a switch without a value falls through to the variable', () => {
  const env = { MINDS_PROFILE_FILE: '/home/alice/profiles/dev.json' };
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile='], env }), '/home/alice/profiles/dev.json');
  assert.equal(profilePathFromLaunch({ args: ['/app/Imbue Studio', '--profile'], env }), '/home/alice/profiles/dev.json');
});

test('the backend env entry carries the path, and is absent when profiling is off', () => {
  assert.deepEqual(profileFileEnv('/tmp/share.json'), { MINDS_PROFILE_FILE: '/tmp/share.json' });
  assert.deepEqual(profileFileEnv(null), {});
});

test('Chromium records the curated categories, without the SPA\'s own user-timing marks', () => {
  const options = chromiumTracingOptions();
  assert.deepEqual(options.included_categories, ['toplevel', 'devtools.timeline', 'v8', 'navigation', 'loading']);
  assert.deepEqual(options.excluded_categories, ['blink.user_timing']);
  assert.ok(!options.included_categories.includes('blink.user_timing'));
  assert.equal(options.recording_mode, 'record-until-full');
  assert.equal(options.trace_buffer_size_in_kb, 128 * 1024);
  assert.equal(options.trace_buffer_size_in_kb, CHROMIUM_TRACE_BUFFER_KB);
  assert.deepEqual(options.included_categories, [...CHROMIUM_TRACE_CATEGORIES.included]);
  assert.deepEqual(options.excluded_categories, [...CHROMIUM_TRACE_CATEGORIES.excluded]);
});

test('each call hands out fresh arrays, so a caller cannot change the curated list', () => {
  const first = chromiumTracingOptions();
  first.included_categories.push('blink.user_timing');
  assert.ok(!chromiumTracingOptions().included_categories.includes('blink.user_timing'));
});

test('the sidecar sits beside the profile file', () => {
  assert.equal(sidecarPathFor('/tmp/share.json'), '/tmp/share.json.chromium.json');
});

// A fake set of collaborators recording every call, with the outcome of the
// post chosen per test.
function fakeCollaborators({ post }) {
  const calls = { deleted: [], sidecars: [], log: [] };
  return {
    calls,
    collaborators: {
      stopRecording: async () => '/tmp/chromium-trace.json',
      readFile: async (filePath) => {
        assert.equal(filePath, '/tmp/chromium-trace.json');
        return '{"traceEvents":[]}';
      },
      postTrace: post,
      writeSidecar: async (filePath, document) => { calls.sidecars.push({ filePath, document }); },
      deleteFile: async (filePath) => { calls.deleted.push(filePath); },
      log: (line) => calls.log.push(line),
      sidecarPath: '/tmp/share.json.chromium.json',
    },
  };
}

test('flush: the trace is posted, the temporary file deleted, and no sidecar written', async () => {
  const posted = [];
  const { calls, collaborators } = fakeCollaborators({
    post: async (document) => { posted.push(document); },
  });
  const result = await flushChromiumTrace(collaborators);
  assert.deepEqual(result, { posted: true, sidecarPath: null });
  assert.deepEqual(posted, ['{"traceEvents":[]}']);
  assert.deepEqual(calls.deleted, ['/tmp/chromium-trace.json']);
  assert.deepEqual(calls.sidecars, []);
});

test('flush: a failed post writes the raw document to the sidecar, says so, and still deletes the temporary file', async () => {
  const { calls, collaborators } = fakeCollaborators({
    post: async () => { throw new Error('HTTP 503'); },
  });
  const result = await flushChromiumTrace(collaborators);
  assert.deepEqual(result, { posted: false, sidecarPath: '/tmp/share.json.chromium.json' });
  assert.deepEqual(calls.sidecars, [{ filePath: '/tmp/share.json.chromium.json', document: '{"traceEvents":[]}' }]);
  assert.deepEqual(calls.deleted, ['/tmp/chromium-trace.json']);
  assert.ok(calls.log.some((line) => line.includes('HTTP 503')));
  assert.ok(calls.log.some((line) => line.includes('/tmp/share.json.chromium.json')));
});

test('flush: a post that never answers is abandoned at the timeout, aborted, and the sidecar written', async () => {
  let abortedSignal = null;
  const { calls, collaborators } = fakeCollaborators({
    post: (_document, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => {
        abortedSignal = signal;
        reject(new Error('aborted'));
      });
    }),
  });
  const result = await flushChromiumTrace({ ...collaborators, timeoutMs: 20 });
  assert.deepEqual(result, { posted: false, sidecarPath: '/tmp/share.json.chromium.json' });
  assert.ok(abortedSignal !== null && abortedSignal.aborted);
  assert.equal(calls.sidecars.length, 1);
  assert.deepEqual(calls.deleted, ['/tmp/chromium-trace.json']);
  assert.ok(calls.log.some((line) => line.includes('timed out after 20ms')));
});

test('flush: the default timeout is the spec\'s 30 seconds', () => {
  assert.equal(FLUSH_TIMEOUT_MS, 30000);
});

test('flush: when the sidecar cannot be written either, nothing throws and the result says so', async () => {
  const { calls, collaborators } = fakeCollaborators({
    post: async () => { throw new Error('ECONNREFUSED'); },
  });
  collaborators.writeSidecar = async () => { throw new Error('EACCES'); };
  const result = await flushChromiumTrace(collaborators);
  assert.deepEqual(result, { posted: false, sidecarPath: null });
  assert.deepEqual(calls.deleted, ['/tmp/chromium-trace.json']);
  assert.ok(calls.log.some((line) => line.includes('EACCES')));
});

test('flush: a recording that cannot be stopped, or a trace that cannot be read, is logged and not fatal', async () => {
  const stopFailure = fakeCollaborators({ post: async () => {} });
  stopFailure.collaborators.stopRecording = async () => { throw new Error('not recording'); };
  assert.deepEqual(await flushChromiumTrace(stopFailure.collaborators), { posted: false, sidecarPath: null });
  assert.deepEqual(stopFailure.calls.deleted, []);
  assert.ok(stopFailure.calls.log.some((line) => line.includes('not recording')));

  const readFailure = fakeCollaborators({ post: async () => {} });
  readFailure.collaborators.readFile = async () => { throw new Error('ENOENT'); };
  assert.deepEqual(await flushChromiumTrace(readFailure.collaborators), { posted: false, sidecarPath: null });
  assert.deepEqual(readFailure.calls.deleted, ['/tmp/chromium-trace.json']);
  assert.ok(readFailure.calls.log.some((line) => line.includes('ENOENT')));
});

test('flush: a failure to delete the temporary file is logged, not thrown', async () => {
  const { calls, collaborators } = fakeCollaborators({ post: async () => {} });
  collaborators.deleteFile = async () => { throw new Error('EPERM'); };
  assert.deepEqual(await flushChromiumTrace(collaborators), { posted: true, sidecarPath: null });
  assert.ok(calls.log.some((line) => line.includes('EPERM')));
});
