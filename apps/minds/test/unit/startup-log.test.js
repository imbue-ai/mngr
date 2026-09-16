// Unit tests for the loading document's startup log rules.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { STARTUP_LOG_MAX_LINES, isSecretStartupLogLine, appendStartupLogLine } = require('../../electron/startup-log');

test('lines carrying a credential are kept off the page', () => {
  assert.equal(isSecretStartupLogLine('Minds login URL (one-time use): http://localhost:1/login?one_time_code=abc'), true);
  assert.equal(isSecretStartupLogLine('Spawning mngr forward --preauth-cookie *** --browser-bridge-token ***'), true);
  assert.equal(isSecretStartupLogLine('Installed 12 packages in 1.2s'), false);
});

test('the log keeps only the newest lines', () => {
  const lines = [];
  for (let index = 0; index < STARTUP_LOG_MAX_LINES + 5; index += 1) appendStartupLogLine(lines, `line ${index}`);
  assert.equal(lines.length, STARTUP_LOG_MAX_LINES);
  assert.equal(lines[0], 'line 5');
  assert.equal(lines[lines.length - 1], `line ${STARTUP_LOG_MAX_LINES + 4}`);
});
