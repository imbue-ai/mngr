// Unit tests for the startup error raised when the backend dies before its
// login URL.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The message is all a backend-down report carries in its event body, so these
// pin that it keeps the backend's dying stderr (bounded, newest last, credentials
// and the home directory removed) and the launch mode alongside the exit status.

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  STDERR_TAIL_MAX_LINES,
  STDERR_TAIL_MAX_CHARS,
  createStderrTail,
  describeLaunch,
  formatEarlyExitDetails,
  formatSpawnFailureDetails,
} = require('../../electron/backend-exit-diagnostics');

const HOME = '/Users/example';
const DEV_LAUNCH = describeLaunch({ isDev: true, hasConfigFileArg: false, hasInheritedClientConfigEnv: true });

test('the tail keeps the newest lines, oldest dropped first', () => {
  const tail = createStderrTail({ homeDir: HOME });
  for (let index = 0; index < STDERR_TAIL_MAX_LINES + 3; index += 1) tail.push(`line ${index}`);
  const lines = tail.lines();
  assert.equal(lines.length, STDERR_TAIL_MAX_LINES);
  assert.equal(lines[0], 'line 3');
  assert.equal(lines[lines.length - 1], `line ${STDERR_TAIL_MAX_LINES + 2}`);
});

test('the tail stays under its size ceiling, even for one oversized line', () => {
  const tail = createStderrTail({ homeDir: HOME });
  tail.push('x'.repeat(STDERR_TAIL_MAX_CHARS - 5));
  tail.push('final line');
  assert.deepEqual(tail.lines(), ['final line']);

  const oversized = createStderrTail({ homeDir: HOME });
  oversized.push(`${'a'.repeat(STDERR_TAIL_MAX_CHARS)}END`);
  const [kept] = oversized.lines();
  assert.equal(kept.length, STDERR_TAIL_MAX_CHARS);
  assert.ok(kept.endsWith('END'));
});

test('the tail skips blank and credential-bearing lines', () => {
  const tail = createStderrTail({ homeDir: HOME });
  tail.push('');
  tail.push('   ');
  tail.push('Minds login URL (one-time use): http://localhost:1/login?one_time_code=abc');
  tail.push('error: Failed to parse `uv.lock`');
  assert.deepEqual(tail.lines(), ['error: Failed to parse `uv.lock`']);
});

test('the tail replaces the home directory with ~', () => {
  const tail = createStderrTail({ homeDir: HOME });
  tail.push(`  File "${HOME}/src/mngr/apps/minds/imbue/minds/main.py", line 3, in <module>`);
  assert.deepEqual(tail.lines(), ['  File "~/src/mngr/apps/minds/imbue/minds/main.py", line 3, in <module>']);
});

test('the tail leaves a path that merely starts with the home directory\'s name alone', () => {
  const tail = createStderrTail({ homeDir: '/home/al' });
  tail.push('cwd /home/al, reading /home/alice/project/uv.lock and /home/al/.venv');
  assert.deepEqual(tail.lines(), ['cwd ~, reading /home/alice/project/uv.lock and ~/.venv']);
});

test('describeLaunch names the mode and config source without any path', () => {
  assert.equal(DEV_LAUNCH, 'launch: dev, --config-file: no, MINDS_CLIENT_CONFIG_PATH inherited: yes');
  assert.equal(
    describeLaunch({ isDev: false, hasConfigFileArg: true, hasInheritedClientConfigEnv: false }),
    'launch: packaged, --config-file: yes, MINDS_CLIENT_CONFIG_PATH inherited: no'
  );
});

test('an early exit keeps the established summary first, then the diagnosis', () => {
  const details = formatEarlyExitDetails({
    code: 1,
    signal: null,
    elapsedMs: 987,
    stdoutEventCount: 0,
    launch: DEV_LAUNCH,
    stderrLines: ['Traceback (most recent call last):', 'ModuleNotFoundError: No module named example'],
  });
  assert.equal(
    details,
    [
      'Backend exited with code 1 before emitting login URL',
      'exited 987 ms after spawn; stdout JSONL events seen: 0',
      DEV_LAUNCH,
      'last stderr lines:',
      '  Traceback (most recent call last):',
      '  ModuleNotFoundError: No module named example',
    ].join('\n')
  );
});

test('an early exit by signal names the signal, and says so when stderr was silent', () => {
  const details = formatEarlyExitDetails({
    code: null,
    signal: 'SIGKILL',
    elapsedMs: 5,
    stdoutEventCount: 2,
    launch: DEV_LAUNCH,
    stderrLines: [],
  });
  assert.match(details, /^Backend exited with code null \(signal SIGKILL\) before emitting login URL\n/);
  assert.match(details, /stdout JSONL events seen: 2/);
  assert.match(details, /\nno stderr output$/);
});

test('a spawn failure carries the launch mode', () => {
  assert.equal(
    formatSpawnFailureDetails({ errorMessage: 'spawn uv ENOENT', launch: DEV_LAUNCH }),
    `Failed to start backend: spawn uv ENOENT\n${DEV_LAUNCH}`
  );
});
