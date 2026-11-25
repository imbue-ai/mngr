// Unit tests for what of the backend's stdout is masked on its way to minds.log.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const { redactStdoutLineForLog } = require('../../electron/backend-stdout-redaction');

test("the backend's session preauth cookie never reaches the log, and the rest of its login_url event does", () => {
  const preauthCookie = 'Zx9-s3ss10n_pr3auth';
  // The event as `minds run` emits it to a desktop app that started it.
  const line = JSON.stringify({
    login_url: 'http://localhost:8420/',
    preauth_cookie: preauthCookie,
    event: 'login_url',
  });

  const logged = redactStdoutLineForLog(line);

  assert.equal(logged.includes(preauthCookie), false);
  assert.deepEqual(JSON.parse(logged), {
    login_url: 'http://localhost:8420/',
    preauth_cookie: '***',
    event: 'login_url',
  });
});

test("the forward's preauth cookie never reaches the log, and the rest of its mngr_forward_started event does", () => {
  const preauthCookie = 'Qw7-f0rward_pr3auth';
  const line = JSON.stringify({ event: 'mngr_forward_started', port: 8421, preauth_cookie: preauthCookie });

  const logged = redactStdoutLineForLog(line);

  assert.equal(logged.includes(preauthCookie), false);
  assert.deepEqual(JSON.parse(logged), { event: 'mngr_forward_started', port: 8421, preauth_cookie: '***' });
});

test('a line that is not a secret-bearing event is logged byte for byte', () => {
  const unchangedLines = [
    // An event with no secret field, spaced as Python's json.dumps writes it.
    '{"event": "notification", "title": "Done", "preauth_cookie": "not a secret here"}',
    'Traceback (most recent call last):',
    '{"truncated": ',
    '[1, 2, 3]',
    'null',
    '',
    '   ',
  ];
  for (const line of unchangedLines) {
    assert.equal(redactStdoutLineForLog(line), line);
  }
});
