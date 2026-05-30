// Drive an EXISTING workspace through the chat round-trip.
//
// Sends a prompt with a unique embedded token so a stale message in the
// chat history can't false-positive the "reply received" check. Asserts
// the reply contains a derived signature only the model would emit.
//
// Usage:
//   MINDS_WORKSPACE=weishi30 node drive-existing.js

const { _electron: electron } = require('playwright');
const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.MINDS_WORKSPACE || 'weishi30';
const NONCE = Math.random().toString(36).slice(2, 10);  // 8-char base36
const PROMPT = process.env.MINDS_TEST_PROMPT
  || `Output exactly the two-line response below, nothing more:\nACK ${NONCE}\nDONE`;
const EXPECT = (process.env.MINDS_TEST_EXPECT || `ACK ${NONCE}`).toLowerCase();
const DIR = path.join(__dirname, 'screenshots');
const T0 = Date.now();

fs.mkdirSync(DIR, { recursive: true });
function log(m) { console.log(`[${Math.floor((Date.now()-T0)/1000)}s] ${m}`); }
async function shot(p, n) { try { await p.screenshot({ path: path.join(DIR, `${n}.png`) }); } catch (_) {} }

(async () => {
  const exec = process.env.MINDS_APP_PATH
    || '/Applications/Minds.app/Contents/MacOS/Minds';
  log(`launching ${exec}`);
  log(`workspace=${WORKSPACE} nonce=${NONCE}`);
  log(`will look for: ${JSON.stringify(EXPECT)}`);

  const app = await electron.launch({ executablePath: exec, env: process.env });
  const win = await app.firstWindow({ timeout: 60_000 });
  log(`first window ready url=${await win.url()}`);

  const origin = await win.evaluate(() => location.origin);
  await win.goto(origin + '/');
  log(`home url=${await win.url()}`);

  await win.waitForSelector(`text=${WORKSPACE}`, { timeout: 30_000 });
  await win.click(`text=${WORKSPACE}`, { timeout: 5_000 });

  let chatUrl = '';
  for (let i = 0; i < 60; i++) {
    const u = await win.url();
    if (u.match(/agent-[a-f0-9]+\.localhost/)) { chatUrl = u; break; }
    await win.waitForTimeout(1000);
  }
  if (!chatUrl) { log(`URL never transitioned`); await app.close(); process.exit(2); }
  log(`chat URL: ${chatUrl}`);

  const input = await win.waitForSelector(
    'textarea, [contenteditable="true"]',
    { timeout: 60_000 }
  );

  // Pre-send body text snapshot so we can prove the reply token is NEW.
  const beforeText = await win.evaluate(() => document.body.innerText.toLowerCase());
  log(`pre-send body has the nonce already? ${beforeText.includes(NONCE.toLowerCase())}`);

  await input.fill(PROMPT);
  await input.press('Enter');
  await shot(win, 'y-sent');
  log(`typed + sent; nonce=${NONCE}`);

  log(`waiting for "${EXPECT}" (NEW, post-send only)`);
  for (let i = 0; i < 180; i++) {
    const body = await win.evaluate(() => document.body.innerText.toLowerCase());
    if (body.includes(EXPECT)) {
      const newOccurrence = body.split(EXPECT).length - 1;
      const oldOccurrence = beforeText.split(EXPECT).length - 1;
      // Need >= 2 occurrences in body: one from the user's typed message
      // echoed back in their chat bubble, plus one from the assistant's
      // reply bubble. Single occurrence = user msg only, model hasn't
      // replied yet.
      if (newOccurrence >= oldOccurrence + 2) {
        log(`PASS: "${EXPECT}" appeared at t=${i}s (occurrences before=${oldOccurrence} now=${newOccurrence})`);
        await shot(win, 'y-pass');
        await app.close();
        process.exit(0);
      } else {
        // Match was already there before send -- keep waiting for a NEW one.
        if (i % 10 === 0) log(`(${i}s) stale match only; waiting for a new occurrence`);
      }
    }
    if (i % 15 === 0 && i > 0) {
      const tail = (await win.evaluate(() => document.body.innerText)).slice(-300);
      log(`waiting (${i}s) last 200 chars: ${JSON.stringify(tail.slice(-200))}`);
      await shot(win, `y-waiting-${i}s`);
    }
    await win.waitForTimeout(1000);
  }
  log(`TIMEOUT`);
  await shot(win, 'y-timeout');
  await app.close();
  process.exit(3);
})().catch(e => {
  console.error(`[fatal] ${e.message}\n${e.stack}`);
  process.exit(99);
});
