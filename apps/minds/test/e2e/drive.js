// Step-by-step autonomous UI driver: launches Electron via Playwright,
// performs ONE action, screenshots, logs, then exits. Designed for tight
// dev-loop iteration where I want to *see* exactly what's happening
// after each step instead of waiting for a multi-minute test to fail
// silently.
//
// Usage:
//   node drive.js <step>
//   where <step> is one of:
//     0  - launch, screenshot, dump url + visible elements
//     1  - goto /create + screenshot the form
//     2  - reveal advanced + fill name/branch + select LIMA + screenshot
//     3  - click create-submit + poll URL until it changes
//     4  - wait for #content-frame to attach + screenshot
//     5  - dump iframe document + find chat input
//     6  - send 'ping' + watch for 'pong'
//
// Each step writes ./screenshots/<step>.png and dumps relevant DOM state
// to ./screenshots/<step>.txt so I can review without scrolling logs.

const { _electron: electron } = require('playwright');
const fs = require('fs');
const path = require('path');

const STEP = parseInt(process.argv[2] || '0', 10);
const DIR = path.join(__dirname, 'screenshots');
const HOST_NAME = process.env.MINDS_TEST_HOST_NAME
  || `pw-${Date.now().toString(36)}`;
const BRANCH = process.env.MINDS_TEST_BRANCH || 'pilot';

fs.mkdirSync(DIR, { recursive: true });
function log(msg) { console.log(`[drive] ${msg}`); }
function shot(page, name) {
  return page.screenshot({ path: path.join(DIR, `${name}.png`), fullPage: false });
}
function dump(name, text) {
  fs.writeFileSync(path.join(DIR, `${name}.txt`), text);
}

async function step0(window) {
  log(`url=${await window.url()}`);
  await shot(window, '0-launch');
  const tree = await window.accessibility.snapshot();
  dump('0-launch-a11y', JSON.stringify(tree, null, 2).slice(0, 4000));
  log('captured screenshots/0-launch.{png,txt}');
}

async function step1(window) {
  const origin = await window.evaluate(() => location.origin);
  log(`origin=${origin}`);
  await window.goto(`${origin}/create`);
  await window.waitForSelector('#create-form', { timeout: 30_000 });
  await shot(window, '1-create-form');
  log('captured screenshots/1-create-form.png');
}

async function step2(window) {
  // Make sure we're at /create
  const url = await window.url();
  log(`current url=${url}`);
  if (!url.includes('/create')) {
    const origin = await window.evaluate(() => location.origin);
    await window.goto(`${origin}/create`);
  }
  await window.waitForSelector('#create-form', { timeout: 30_000 });
  await window.click('#toggle-advanced');
  await window.waitForTimeout(300);
  await window.fill('#host_name', HOST_NAME);
  await window.fill('#branch', BRANCH);
  await window.selectOption('#launch_mode', 'LIMA');
  await shot(window, '2-form-filled');
  log(`form filled: host_name=${HOST_NAME} branch=${BRANCH} launch_mode=LIMA`);
}

async function step3(window) {
  // Need to redo step 2 since each `node drive.js N` is a fresh Electron instance.
  await step2(window);
  const beforeUrl = await window.url();
  await window.click('#create-submit');
  log('clicked Create; polling URL change');
  for (let i = 0; i < 30; i++) {
    await window.waitForTimeout(1000);
    const u = await window.url();
    if (u !== beforeUrl) {
      log(`URL changed: ${beforeUrl} -> ${u}`);
      break;
    }
  }
  await shot(window, '3-after-submit');
  log(`final url=${await window.url()}`);
}

async function step4(window) {
  // Watch for content-frame in the URL we already landed on.
  log(`url=${await window.url()}`);
  for (let i = 0; i < 30; i++) {
    const cf = await window.$('#content-frame');
    if (cf) {
      const src = await cf.getAttribute('src');
      log(`content-frame attached after ${i}s, src=${src}`);
      await shot(window, `4-content-frame-${i}s`);
      return;
    }
    await window.waitForTimeout(1000);
  }
  log('content-frame never appeared');
  await shot(window, '4-no-content-frame');
}

async function step5(window) {
  await window.waitForSelector('#content-frame', { timeout: 30_000 });
  const frame = window.frame({ name: 'content-frame' })
    || window.frames().find(f => f.url().match(/localhost|127\.0\.0\.1/));
  if (!frame) {
    log('no in-window frame matched');
    const frames = window.frames();
    log(`available frames: ${frames.length}`);
    for (const f of frames) log(`  ${f.url()}`);
    return;
  }
  log(`chat frame url: ${frame.url()}`);
  await shot(window, '5-chat-loaded');
  const html = await frame.content();
  dump('5-chat-html', html.slice(0, 8000));
}

async function step6(window) {
  await window.waitForSelector('#content-frame', { timeout: 30_000 });
  const frame = window.frames().find(f => f.url().match(/localhost|127\.0\.0\.1/));
  const input = await frame.waitForSelector(
    'textarea, [contenteditable="true"]',
    { timeout: 60_000 }
  );
  await input.fill(process.env.MINDS_TEST_PROMPT
    || 'Reply with exactly the four characters: pong');
  await input.press('Enter');
  await shot(window, '6-sent');
  log('sent; polling for "pong"');
  for (let i = 0; i < 120; i++) {
    const found = await frame.evaluate(() =>
      document.body.innerText.toLowerCase().includes('pong'));
    if (found) {
      log(`pong appeared after ${i}s`);
      await shot(window, `6-pong-${i}s`);
      return;
    }
    await window.waitForTimeout(1000);
  }
  log('pong never appeared');
}

(async () => {
  const exec = process.env.MINDS_APP_PATH
    || '/Applications/Minds.app/Contents/MacOS/Minds';
  log(`launching ${exec}`);
  const app = await electron.launch({ executablePath: exec, env: process.env });
  const win = await app.firstWindow({ timeout: 60_000 });
  log('first window ready');

  try {
    const steps = { 0: step0, 1: step1, 2: step2, 3: step3, 4: step4, 5: step5, 6: step6 };
    const fn = steps[STEP];
    if (!fn) { log(`unknown step ${STEP}`); process.exit(1); }
    await fn(win);
  } catch (e) {
    log(`STEP ${STEP} threw: ${e.message}`);
    await shot(win, `${STEP}-error`).catch(() => {});
    dump(`${STEP}-error`, e.stack || e.message);
  } finally {
    // Leave the window open briefly so user can see end state, then close.
    await win.waitForTimeout(2000);
    await app.close().catch(() => {});
  }
})();
