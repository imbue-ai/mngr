// Full create+chat driver with active observation.
//
// Stays alive through the entire create -> lima boot -> bootstrap -> chat
// flow, printing progress every 15 seconds (URL, visible status text,
// screenshot path).
//
// Usage:
//   MINDS_TEST_PROMPT="..." MINDS_TEST_EXPECT="..." node drive-full.js
//
// Defaults:
//   workspace name = pw-<timestamp>
//   branch         = pilot
//   compute        = LIMA
//   AI provider    = subscription (default)
//   prompt         = ping -> pong

const { _electron: electron } = require('playwright');
const fs = require('fs');
const path = require('path');

const HOST_NAME = process.env.MINDS_TEST_HOST_NAME
  || `pw-${Date.now().toString(36)}`;
const BRANCH = process.env.MINDS_TEST_BRANCH || 'pilot';
const PROMPT = process.env.MINDS_TEST_PROMPT
  || 'Reply with exactly the four characters: pong';
const EXPECT = (process.env.MINDS_TEST_EXPECT || 'pong').toLowerCase();
const DIR = path.join(__dirname, 'screenshots');
const T0 = Date.now();

fs.mkdirSync(DIR, { recursive: true });
function log(msg) { console.log(`[${Math.floor((Date.now()-T0)/1000)}s] ${msg}`); }
async function shot(page, name) {
  try { await page.screenshot({ path: path.join(DIR, `${name}.png`) }); }
  catch (_) {}
}

(async () => {
  const exec = process.env.MINDS_APP_PATH
    || '/Applications/Minds.app/Contents/MacOS/Minds';
  log(`launching ${exec}`);
  const app = await electron.launch({ executablePath: exec, env: process.env });
  const win = await app.firstWindow({ timeout: 60_000 });
  log(`first window ready (url=${await win.url()})`);
  await shot(win, 'a-launch');

  // Reset session to /create regardless of where Electron restored to.
  const origin = await win.evaluate(() => location.origin);
  log(`origin=${origin}; navigating to /create`);
  await win.goto(`${origin}/create`);
  await win.waitForSelector('#create-form', { timeout: 30_000 });
  log('create form visible');

  log('revealing advanced + filling form');
  await win.click('#toggle-advanced');
  await win.fill('#host_name', HOST_NAME);
  await win.fill('#branch', BRANCH);
  await win.selectOption('#launch_mode', 'LIMA');
  await shot(win, 'b-form');
  log(`form filled: name=${HOST_NAME} branch=${BRANCH} launch_mode=LIMA`);

  log('clicking Create button');
  await win.click('#create-submit');

  // Phase 1: wait for URL to transition off /create.
  log('phase 1: waiting for redirect off /create');
  for (let i = 0; i < 30; i++) {
    await win.waitForTimeout(1000);
    const u = await win.url();
    if (!u.endsWith('/create')) {
      log(`-> url now ${u}`);
      break;
    }
  }

  // Phase 2: poll /creating/<id> for progress until URL settles on
  // /<host_name> or we hit max budget. Every 15s log + screenshot.
  log(`phase 2: waiting for workspace ready (max 15 min)`);
  const PHASE2_BUDGET = 15 * 60 * 1000;
  const START = Date.now();
  let lastUrl = '';
  let settled = false;

  while (Date.now() - START < PHASE2_BUDGET) {
    const url = await win.url();
    if (url !== lastUrl) { log(`URL: ${url}`); lastUrl = url; }

    // Check if URL is the workspace landing (contains the host name path).
    if (url.includes(`/${HOST_NAME}`) || url.match(/\/[^/]+\/main\b/)) {
      log(`-> workspace URL detected: ${url}`);
      settled = true;
      break;
    }

    // Snapshot progress: pull any progress text the creating page
    // exposes (status, log lines).
    try {
      const status = await win.evaluate(() => {
        const t = (sel) => document.querySelector(sel)?.textContent?.trim() || '';
        return {
          h1: t('h1'),
          status: t('#creation-status') || t('[data-status]'),
          log_tail: t('#creation-log')?.split('\n').slice(-3).join('\n') || '',
        };
      });
      const snap = JSON.stringify(status);
      if (snap !== '{"h1":"","status":"","log_tail":""}') {
        log(`page: ${snap.slice(0, 200)}`);
      }
    } catch (_) {}

    await shot(win, `c-progress-${Math.floor((Date.now()-START)/1000)}s`);
    await win.waitForTimeout(15_000);
  }

  if (!settled) {
    log(`PHASE 2 TIMEOUT after ${Math.floor((Date.now()-START)/1000)}s. Bailing.`);
    await shot(win, 'd-timeout');
    await app.close();
    process.exit(2);
  }

  // Phase 3: find content-frame iframe + send chat message.
  log('phase 3: waiting for #content-frame');
  for (let i = 0; i < 30; i++) {
    const cf = await win.$('#content-frame');
    if (cf) {
      const src = await cf.getAttribute('src');
      log(`content-frame attached, src=${src}`);
      break;
    }
    await win.waitForTimeout(1000);
  }
  await shot(win, 'e-chat-loaded');

  // Find the chat input inside the iframe.
  const chatFrame = win.frames().find(f => f.url() && f.url() !== 'about:blank');
  if (!chatFrame) {
    log('no chat iframe found');
    await app.close();
    process.exit(3);
  }
  log(`chat frame url: ${chatFrame.url()}`);

  log('waiting for chat input');
  const input = await chatFrame.waitForSelector(
    'textarea, [contenteditable="true"]',
    { timeout: 3 * 60 * 1000 }
  );
  log('chat input visible; typing prompt');
  await input.fill(PROMPT);
  await input.press('Enter');
  await shot(win, 'f-sent');

  log(`waiting for "${EXPECT}" in reply (max 3 min)`);
  const REPLY_BUDGET = 3 * 60 * 1000;
  const REPLY_START = Date.now();
  while (Date.now() - REPLY_START < REPLY_BUDGET) {
    const found = await chatFrame.evaluate((needle) => {
      return document.body.innerText.toLowerCase().includes(needle);
    }, EXPECT);
    if (found) {
      log(`-> "${EXPECT}" found after ${Math.floor((Date.now()-REPLY_START)/1000)}s`);
      await shot(win, 'g-pong');
      log('PASS');
      await app.close();
      process.exit(0);
    }
    await shot(win, `f-waiting-${Math.floor((Date.now()-REPLY_START)/1000)}s`);
    await win.waitForTimeout(5_000);
  }
  log(`REPLY TIMEOUT. Bailing.`);
  await shot(win, 'g-noreply');
  await app.close();
  process.exit(4);
})().catch(e => {
  console.error(`[fatal] ${e.message}`);
  console.error(e.stack);
  process.exit(99);
});
