// Unit tests for the keychain helper. Runnable with `node --test
// apps/minds/electron/keychain.test.js` -- depends only on Node's built-in
// `node:test` module so we don't add a JS test framework to the project.
//
// The helper takes execFile and platform via options so we can drive every
// branch from process-local fakes without spawning a real `security`
// subprocess.

const test = require('node:test');
const assert = require('node:assert');

const {
  CLAUDE_CODE_KEYCHAIN_LABEL,
  KEYCHAIN_READ_TIMEOUT_MS,
  readKeychainCredential,
  readClaudeCodeApiKey,
} = require('./keychain');

function makeExecFileFake(onCall) {
  return (file, args, options, cb) => {
    onCall({ file, args, options });
    cb(null, 'sk-ant-fake-key\n');
  };
}

test('readKeychainCredential returns null on non-darwin without invoking execFile', async () => {
  let called = false;
  const execFileImpl = () => {
    called = true;
  };
  const result = await readKeychainCredential('Claude Code', { execFileImpl, platform: 'linux' });
  assert.strictEqual(result, null);
  assert.strictEqual(called, false);
});

test('readKeychainCredential invokes /usr/bin/security with the requested label', async () => {
  let captured = null;
  const execFileImpl = makeExecFileFake((call) => { captured = call; });
  await readKeychainCredential('Claude Code', { execFileImpl, platform: 'darwin' });
  assert.ok(captured, 'expected execFile to be called');
  assert.strictEqual(captured.file, '/usr/bin/security');
  assert.deepStrictEqual(captured.args, ['find-generic-password', '-l', 'Claude Code', '-w']);
  assert.strictEqual(captured.options.timeout, KEYCHAIN_READ_TIMEOUT_MS);
});

test('readKeychainCredential returns trimmed stdout on success', async () => {
  const execFileImpl = (_file, _args, _options, cb) => cb(null, '  sk-ant-trimmed-key  \n');
  const result = await readKeychainCredential('Claude Code', { execFileImpl, platform: 'darwin' });
  assert.strictEqual(result, 'sk-ant-trimmed-key');
});

test('readKeychainCredential returns null when execFile errors (missing entry / denied / timeout)', async () => {
  const execFileImpl = (_file, _args, _options, cb) => {
    const err = new Error('SecKeychainSearchCopyNext: The specified item could not be found.');
    err.code = 44;
    cb(err, '');
  };
  const result = await readKeychainCredential('Claude Code', { execFileImpl, platform: 'darwin' });
  assert.strictEqual(result, null);
});

test('readKeychainCredential returns null when stdout is empty/whitespace-only', async () => {
  const execFileImpl = (_file, _args, _options, cb) => cb(null, '   \n');
  const result = await readKeychainCredential('Claude Code', { execFileImpl, platform: 'darwin' });
  assert.strictEqual(result, null);
});

test('readClaudeCodeApiKey targets the "Claude Code" label', async () => {
  let captured = null;
  const execFileImpl = makeExecFileFake((call) => { captured = call; });
  await readClaudeCodeApiKey({ execFileImpl, platform: 'darwin' });
  assert.ok(captured.args.includes(CLAUDE_CODE_KEYCHAIN_LABEL));
});

test('CLAUDE_CODE_KEYCHAIN_LABEL matches the wire-format label mngr_claude reads', () => {
  // Both sides of the wire-format contract -- the Python side reads the same
  // label name. Pinning here so a rename has to update both sides.
  assert.strictEqual(CLAUDE_CODE_KEYCHAIN_LABEL, 'Claude Code');
});
