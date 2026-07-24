// Unit tests for minds:// deeplink parsing.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The parsing logic
// is the pure electron/deeplink.js module, deliberately split out of main.js
// (which can't be required outside Electron) so it is testable here. What is
// NOT covered (main.js wiring, verified manually): protocol registration,
// open-url / second-instance event delivery, the pending-deeplink queue and
// its flush ordering against startup, and window focus semantics.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  parseDeeplink,
  deeplinkTargetPath,
  extractDeeplinkUrlFromArgv,
  MAX_DEEPLINK_LENGTH,
} = require('../../electron/deeplink');

// -- parseDeeplink / deeplinkTargetPath --

test('bare minds:// -> focus only', () => {
  assert.deepEqual(parseDeeplink('minds://'), { action: 'focus' });
  assert.equal(deeplinkTargetPath('minds://'), null);
});

test('minds:create without slashes has no host -> focus only', () => {
  assert.deepEqual(parseDeeplink('minds:create'), { action: 'focus' });
  assert.equal(deeplinkTargetPath('minds:create'), null);
});

test('minds://create with no params -> create page without query', () => {
  assert.deepEqual(parseDeeplink('minds://create'), { action: 'create', gitUrl: '', branch: '' });
  assert.equal(deeplinkTargetPath('minds://create'), '/create');
});

test('git_url only', () => {
  const url = 'minds://create?git_url=https%3A%2F%2Fgithub.com%2Fimbue-ai%2Fexample';
  assert.deepEqual(parseDeeplink(url), {
    action: 'create',
    gitUrl: 'https://github.com/imbue-ai/example',
    branch: '',
  });
  // A repo-carrying link is an Inspiration link: it targets the Create from
  // Inspiration page rather than the plain create form.
  assert.equal(deeplinkTargetPath(url), '/create/inspiration?git_url=https%3A%2F%2Fgithub.com%2Fimbue-ai%2Fexample');
});

test('branch only', () => {
  assert.equal(deeplinkTargetPath('minds://create?branch=main'), '/create?branch=main');
});

test('both params', () => {
  const url = 'minds://create?git_url=https%3A%2F%2Fgithub.com%2Fa%2Fb&branch=v1.2.3';
  assert.deepEqual(parseDeeplink(url), {
    action: 'create',
    gitUrl: 'https://github.com/a/b',
    branch: 'v1.2.3',
  });
  assert.equal(deeplinkTargetPath(url), '/create/inspiration?git_url=https%3A%2F%2Fgithub.com%2Fa%2Fb&branch=v1.2.3');
});

test('empty-string params behave like absent ones', () => {
  assert.equal(deeplinkTargetPath('minds://create?git_url=&branch='), '/create');
  assert.equal(deeplinkTargetPath('minds://create?git_url=%20%20'), '/create');
});

test('git_url containing its own query survives the decode/re-encode round trip', () => {
  // ?token=abc inside the value, percent-encoded by the sender.
  const url = 'minds://create?git_url=https%3A%2F%2Fhost%2Frepo.git%3Ftoken%3Dabc';
  assert.equal(parseDeeplink(url).gitUrl, 'https://host/repo.git?token=abc');
  assert.equal(deeplinkTargetPath(url), '/create/inspiration?git_url=https%3A%2F%2Fhost%2Frepo.git%3Ftoken%3Dabc');
});

test('branch with slash and space is re-encoded', () => {
  assert.equal(deeplinkTargetPath('minds://create?branch=feat%2Fx%20y'), '/create?branch=feat%2Fx+y');
});

test('unknown params are ignored', () => {
  assert.equal(deeplinkTargetPath('minds://create?branch=main&evil=1&commit=abc'), '/create?branch=main');
});

test('scheme and host are case-insensitive', () => {
  // Non-special schemes keep host case in the URL parser, so the explicit
  // lowercase in parseDeeplink is load-bearing.
  assert.equal(deeplinkTargetPath('MINDS://CREATE?branch=main'), '/create?branch=main');
});

test('extra path segments after the action host are ignored', () => {
  assert.equal(deeplinkTargetPath('minds://create/extra?branch=main'), '/create?branch=main');
});

test('unknown hosts -> focus only', () => {
  assert.deepEqual(parseDeeplink('minds://open?page=create'), { action: 'focus' });
  assert.deepEqual(parseDeeplink('minds://nonsense?x=1'), { action: 'focus' });
  assert.deepEqual(parseDeeplink('minds://%'), { action: 'focus' });
});

test('non-minds schemes -> focus only', () => {
  assert.deepEqual(parseDeeplink('https://create?git_url=x'), { action: 'focus' });
  assert.deepEqual(parseDeeplink('file:///create'), { action: 'focus' });
});

test('garbage and non-string input never throws -> focus only', () => {
  for (const bad of [':::', '', null, undefined, 42, {}, ['minds://create']]) {
    assert.deepEqual(parseDeeplink(bad), { action: 'focus' });
    assert.equal(deeplinkTargetPath(bad), null);
  }
});

test('over-length URLs are rejected', () => {
  const long = 'minds://create?branch=' + 'a'.repeat(MAX_DEEPLINK_LENGTH);
  assert.deepEqual(parseDeeplink(long), { action: 'focus' });
  // At the cap it still parses.
  const atCap = 'minds://create?branch=' + 'a'.repeat(MAX_DEEPLINK_LENGTH - 'minds://create?branch='.length);
  assert.equal(atCap.length, MAX_DEEPLINK_LENGTH);
  assert.equal(parseDeeplink(atCap).action, 'create');
});

test('allowlist property: output is null or starts with /create', () => {
  const inputs = [
    'minds://',
    'minds://create',
    'minds://create?git_url=x&branch=y',
    'minds://create/../../etc/passwd',
    'minds://create?git_url=javascript%3Aalert(1)',
    'minds://elsewhere',
    'https://example.com',
    ':::',
    null,
  ];
  for (const input of inputs) {
    const out = deeplinkTargetPath(input);
    assert.ok(
      out === null || out === '/create' || out.startsWith('/create?') || out.startsWith('/create/inspiration?'),
      `unexpected: ${out}`
    );
  }
});

// -- extractDeeplinkUrlFromArgv --

test('finds the URL among binary path, app path, and switches', () => {
  const argv = ['/usr/bin/electron', '--no-sandbox', '.', 'minds://create?git_url=x'];
  assert.equal(extractDeeplinkUrlFromArgv(argv), 'minds://create?git_url=x');
});

test('argv without a URL -> null', () => {
  assert.equal(extractDeeplinkUrlFromArgv(['/usr/bin/electron', '.', '--flag']), null);
  assert.equal(extractDeeplinkUrlFromArgv([]), null);
});

test('uppercase scheme is found', () => {
  assert.equal(extractDeeplinkUrlFromArgv(['MINDS://create']), 'MINDS://create');
});

test('first of two URLs wins', () => {
  assert.equal(extractDeeplinkUrlFromArgv(['minds://a', 'minds://b']), 'minds://a');
});

test('non-array and non-string entries are tolerated', () => {
  assert.equal(extractDeeplinkUrlFromArgv(null), null);
  assert.equal(extractDeeplinkUrlFromArgv(undefined), null);
  assert.equal(extractDeeplinkUrlFromArgv('minds://a'), null);
  assert.equal(extractDeeplinkUrlFromArgv([42, null, 'minds://ok']), 'minds://ok');
});
