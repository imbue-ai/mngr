// CI variant of drive-slack.js: drives the slack permission flow against
// an already-created workspace, then asserts the mock server's canned
// MESSAGE_BODY landed in the assistant's reply (not just the nonce).
//
// Pre-conditions (handled by slack-mock-setup.sh on the same runner):
//   - /etc/hosts inside the lima VM points slack.com / files.slack.com
//     to the macOS host's lima-internal IP.
//   - socat on the host TLS-terminates :443 -> 127.0.0.1:8443.
//   - slack-mock-server.js is running on 127.0.0.1:8443 (plain HTTP).
//   - latchkey inside the VM has a mock slack credential pre-seeded.
//
// Inputs:
//   MINDS_WORKSPACE   workspace tile to click on the home page (required)
//   MINDS_APP_PATH    minds binary (default /Applications/Minds.app/Contents/MacOS/Minds)
//
// Asserts:
//   - The assistant's reply contains the canned MESSAGE_BODY substring
//     `CI MOCK: greetings from the localhost slack mock.`
//
// Exit codes:
//   0  PASS
//   2  workspace tile never transitioned to chat URL
//   3  reply timeout (no canned body in 4 min)
//   99 unhandled error

const { _electron: electron } = require('playwright');
const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.MINDS_WORKSPACE
  || (() => { throw new Error('MINDS_WORKSPACE is required'); })();
const NONCE = Math.random().toString(36).slice(2, 10);
// Must match slack-mock-server.js's MESSAGE_BODY constant.
const CANNED_BODY = 'CI MOCK: greetings from the localhost slack mock.';
const CANNED_BODY_LC = CANNED_BODY.toLowerCase();
const PROMPT = 'Read-only Slack task. DO NOT post, send, or write any '
  + 'message anywhere. Use only read-style Slack tool calls. Read one '
  + 'message from any channel and respond ONLY here in this chat panel '
  + '(no Slack post) with the prefix "TOK ' + NONCE + ':" followed by '
  + 'the EXACT text of the message you read, character-for-character.';
const DIR = path.join(__dirname, 'screenshots');
const T0 = Date.now();

fs.mkdirSync(DIR, { recursive: true });
function log(m) { console.log(`[${Math.floor((Date.now()-T0)/1000)}s] ${m}`); }
async function shot(p, n) { try { await p.screenshot({ path: path.join(DIR, `${n}.png`) }); } catch (_) {} }

async function findApprovalUi(win) {
  const candidates = [
    'button:has-text("Approve")',
    'button:has-text("Allow")',
    'button:has-text("Grant")',
    'a:has-text("Approve")',
    'a:has-text("Allow")',
    '[data-action*="approve" i]',
    '[data-action*="allow" i]',
  ];
  for (const sel of candidates) {
    try {
      const loc = win.locator(sel).first();
      if (await loc.count() > 0 && await loc.isVisible({ timeout: 100 })) {
        return { locator: loc, selector: sel };
      }
    } catch (_) {}
  }
  return null;
}

(async () => {
  const exec = process.env.MINDS_APP_PATH
    || '/Applications/Minds.app/Contents/MacOS/Minds';
  log(`launching ${exec}`);
  log(`workspace=${WORKSPACE} nonce=${NONCE}`);
  log(`asserting canned body: "${CANNED_BODY}"`);

  // Inject brew curl + cacert so the latchkey gateway (running inside
  // this Electron process) makes outbound calls via OpenSSL-built curl
  // that honors --cacert. Macos system curl is SecureTransport-built
  // and would require installing the cert in the System keychain,
  // which needs interactive auth (impossible on a non-TTY runner).
  //
  // Crucially: strip ELECTRON_RUN_AS_NODE from the env. The latchkey
  // shim sets it to run minds.app's binary as a node interpreter, and
  // a runner with that env var set globally (e.g. via .zshenv) would
  // make Electron treat OUR launch as a node script too -- main.js
  // would finish synchronously and the process would exit ~0.2s after
  // `Debugger attached`, with exitCode=0 and no BrowserWindow ever
  // created.
  const env = {
    ...process.env,
    PATH: '/opt/homebrew/opt/curl/bin:' + (process.env.PATH || ''),
    CURL_CA_BUNDLE: '/tmp/slack-mock/cert.pem',
    ELECTRON_RUN_AS_NODE: '',
  };
  delete env.ELECTRON_RUN_AS_NODE;
  const app = await electron.launch({ executablePath: exec, env });
  const win = await app.firstWindow({ timeout: 60_000 });
  const origin = await win.evaluate(() => location.origin);
  await win.goto(origin + '/');
  await win.waitForSelector(`text=${WORKSPACE}`, { timeout: 60_000 });
  await win.click(`text=${WORKSPACE}`, { timeout: 5_000 });

  let chatUrl = '';
  for (let i = 0; i < 60; i++) {
    const u = await win.url();
    if (u.match(/agent-[a-f0-9]+\.localhost/)) { chatUrl = u; break; }
    await win.waitForTimeout(1000);
  }
  if (!chatUrl) {
    log('URL never transitioned to a chat panel');
    await shot(win, 'ci-no-chat-url');
    await app.close();
    process.exit(2);
  }
  log(`chat URL: ${chatUrl}`);

  const input = await win.waitForSelector(
    'textarea, [contenteditable="true"]',
    { timeout: 60_000 }
  );
  const beforeText = await win.evaluate(() => document.body.innerText.toLowerCase());
  const oldCannedOcc = beforeText.split(CANNED_BODY_LC).length - 1;
  log(`pre-send canned-body occurrences: ${oldCannedOcc}`);

  await input.fill(PROMPT);
  await input.press('Enter');
  await shot(win, 'ci-slack-sent');
  log('typed + sent; watching for approval UI and canned body');

  let approvalClicked = false;
  for (let i = 0; i < 240; i++) {
    if (!approvalClicked) {
      const approval = await findApprovalUi(win);
      if (approval) {
        log(`approval UI: "${approval.selector}"; clicking`);
        await shot(win, `ci-slack-approval-${i}s`);
        try {
          await approval.locator.click({ timeout: 3_000 });
          approvalClicked = true;
        } catch (e) {
          log(`approval click failed: ${e.message}`);
        }
      }
    }

    const body = await win.evaluate(() => document.body.innerText.toLowerCase());
    const newCannedOcc = body.split(CANNED_BODY_LC).length - 1;
    if (newCannedOcc > oldCannedOcc) {
      log(`PASS: canned body appeared at t=${i}s (occ ${oldCannedOcc} -> ${newCannedOcc})`);
      await shot(win, 'ci-slack-pass');
      const ctx = await win.evaluate((needle) => {
        const text = document.body.innerText.toLowerCase();
        const idx = text.lastIndexOf(needle);
        return document.body.innerText.slice(Math.max(0, idx - 80), idx + 400);
      }, CANNED_BODY_LC);
      log(`reply context: ${JSON.stringify(ctx)}`);
      await app.close();
      process.exit(0);
    }

    if (i % 15 === 0 && i > 0) {
      const tail = await win.evaluate(() => document.body.innerText.slice(-400));
      log(`waiting (${i}s) approvalClicked=${approvalClicked} tail: ${JSON.stringify(tail.slice(-200))}`);
      await shot(win, `ci-slack-waiting-${i}s`);
    }
    await win.waitForTimeout(1000);
  }
  log('TIMEOUT (4 min)');
  await shot(win, 'ci-slack-timeout');
  await app.close();
  process.exit(3);
})().catch(e => {
  console.error(`[fatal] ${e.message}\n${e.stack}`);
  process.exit(99);
});
