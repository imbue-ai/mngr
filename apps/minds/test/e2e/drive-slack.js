// Drive a slack-tool-call scenario end-to-end through the UI.
//
// Sends "read 1 slack message", watches the chat panel for any
// permission-approval UI (button / link / banner), clicks it, then waits
// for the assistant reply to contain slack-message content (a sender, a
// channel name, or content shape that wouldn't appear pre-send).
//
// Pre-condition: the workspace's latchkey gateway is already authenticated
// with the user's slack account. The user said "i already logged in
// latchkey" so this should be satisfied.
//
// Usage:
//   MINDS_WORKSPACE=weishi30 node drive-slack.js

const { _electron: electron } = require('playwright');
const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.MINDS_WORKSPACE || 'weishi30';
const NONCE = Math.random().toString(36).slice(2, 10);
const PROMPT = process.env.MINDS_SLACK_PROMPT
  || ('Read-only Slack task. DO NOT post, send, or write any message '
    + 'anywhere. Use only read-style Slack tool calls (list_messages, '
    + 'channel_history, etc). After reading, respond ONLY here in this '
    + 'chat panel (no Slack post) with the prefix "TOK ' + NONCE + ':" '
    + 'followed by the sender name and the first 80 characters of one '
    + 'recent message you read.');
const EXPECT_PREFIX = (process.env.MINDS_SLACK_EXPECT || `tok ${NONCE}:`).toLowerCase();
const DIR = path.join(__dirname, 'screenshots');
const T0 = Date.now();

fs.mkdirSync(DIR, { recursive: true });
function log(m) { console.log(`[${Math.floor((Date.now()-T0)/1000)}s] ${m}`); }
async function shot(p, n) { try { await p.screenshot({ path: path.join(DIR, `${n}.png`) }); } catch (_) {} }

// Try to find any approval UI in the page; return a Locator or null.
async function findApprovalUi(win) {
  const candidates = [
    'button:has-text("Approve")',
    'button:has-text("Allow")',
    'button:has-text("Grant")',
    'a:has-text("Approve")',
    'a:has-text("Allow")',
    '[data-action*="approve" i]',
    '[data-action*="allow" i]',
    'text=/Approve/i >> nth=0',
    'text=/Allow/i >> nth=0',
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
  log(`prompt: ${PROMPT.slice(0, 100)}...`);

  const app = await electron.launch({ executablePath: exec, env: process.env });
  const win = await app.firstWindow({ timeout: 60_000 });
  const origin = await win.evaluate(() => location.origin);
  await win.goto(origin + '/');
  await win.waitForSelector(`text=${WORKSPACE}`, { timeout: 30_000 });
  await win.click(`text=${WORKSPACE}`, { timeout: 5_000 });

  let chatUrl = '';
  for (let i = 0; i < 60; i++) {
    const u = await win.url();
    if (u.match(/agent-[a-f0-9]+\.localhost/)) { chatUrl = u; break; }
    await win.waitForTimeout(1000);
  }
  if (!chatUrl) { log('URL never transitioned'); await app.close(); process.exit(2); }
  log(`chat URL: ${chatUrl}`);

  const input = await win.waitForSelector(
    'textarea, [contenteditable="true"]',
    { timeout: 60_000 }
  );

  const beforeText = await win.evaluate(() => document.body.innerText.toLowerCase());
  log(`pre-send body has the nonce? ${beforeText.includes(NONCE.toLowerCase())}`);

  await input.fill(PROMPT);
  await input.press('Enter');
  await shot(win, 'z-sent');
  log('typed + sent; watching for approval UI and reply');

  let approvalClicked = false;
  for (let i = 0; i < 240; i++) {
    // Check for approval UI; click if found.
    if (!approvalClicked) {
      const approval = await findApprovalUi(win);
      if (approval) {
        log(`approval UI found via "${approval.selector}"; clicking`);
        await shot(win, `z-approval-${i}s`);
        try {
          await approval.locator.click({ timeout: 3_000 });
          approvalClicked = true;
          log('approval clicked');
        } catch (e) {
          log(`approval click failed: ${e.message}`);
        }
      }
    }

    // Check for the expected reply token.
    const body = await win.evaluate(() => document.body.innerText.toLowerCase());
    if (body.includes(EXPECT_PREFIX)) {
      const newOcc = body.split(EXPECT_PREFIX).length - 1;
      const oldOcc = beforeText.split(EXPECT_PREFIX).length - 1;
      if (newOcc >= oldOcc + 2) {
        log(`PASS: "${EXPECT_PREFIX}" appeared at t=${i}s (occurrences ${oldOcc} -> ${newOcc})`);
        await shot(win, 'z-pass');
        // Capture the surrounding text for inspection.
        const ctx = await win.evaluate((needle) => {
          const text = document.body.innerText.toLowerCase();
          const idx = text.lastIndexOf(needle);
          return document.body.innerText.slice(Math.max(0, idx - 50), idx + 400);
        }, EXPECT_PREFIX);
        log(`reply context: ${JSON.stringify(ctx)}`);
        await app.close();
        process.exit(0);
      }
    }

    if (i % 15 === 0 && i > 0) {
      const tail = await win.evaluate(() => document.body.innerText.slice(-400));
      log(`waiting (${i}s) approvalClicked=${approvalClicked} last 200 chars: ${JSON.stringify(tail.slice(-200))}`);
      await shot(win, `z-waiting-${i}s`);
    }
    await win.waitForTimeout(1000);
  }
  log('TIMEOUT (4 min)');
  await shot(win, 'z-timeout');
  await app.close();
  process.exit(3);
})().catch(e => {
  console.error(`[fatal] ${e.message}\n${e.stack}`);
  process.exit(99);
});
