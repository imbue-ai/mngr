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

// User's described flow:
//   1. Click chatbox icon in the right side panel (opens the panel).
//   2. Click the permission request entry inside that panel.
//   3. Click "Approve". The panel auto-closes.
// Steps 1+2 are in the main shell window (NOT the chat content frame).
// Search every Electron window. We need a stateful click-progression
// because the buttons are gated on the previous click landing.
async function openRightPanel(app) {
  // Step 1: find and click the chatbox icon (anything that opens the
  // requests panel). Common patterns for icon-only buttons:
  //   - aria-label match
  //   - title attr match
  //   - button containing an svg icon at a known position
  const candidates = [
    '[aria-label*="permission" i]',
    '[aria-label*="request" i]',
    '[aria-label*="chat" i]',
    '[title*="permission" i]',
    '[title*="request" i]',
    'button[aria-label*="message" i]',
    // The user said it's an icon -- try common locations
    'button:has(svg)',
  ];
  for (const w of app.windows()) {
    for (const sel of candidates) {
      try {
        const all = await w.locator(sel).all();
        for (const loc of all) {
          if (!(await loc.isVisible({ timeout: 100 }).catch(() => false))) continue;
          const aria = await loc.getAttribute('aria-label').catch(() => '');
          const title = await loc.getAttribute('title').catch(() => '');
          if (/(permission|request|chat|message)/i.test(`${aria} ${title}`)) {
            return { locator: loc, selector: sel, window: w, aria, title };
          }
        }
      } catch (_) {}
    }
  }
  return null;
}
async function findPermissionRequestEntry(app) {
  for (const w of app.windows()) {
    for (const sel of [
      'text=/slack/i',
      'text=/read.?only/i',
      'text=/permission/i',
      '[data-testid*="permission" i]',
      '[role="listitem"]:has-text("slack")',
    ]) {
      try {
        const loc = w.locator(sel).first();
        if (await loc.count() > 0 && await loc.isVisible({ timeout: 100 }).catch(() => false)) {
          return { locator: loc, selector: sel, window: w };
        }
      } catch (_) {}
    }
  }
  return null;
}
async function findApproveButton(app) {
  for (const w of app.windows()) {
    for (const sel of [
      'button:has-text("Approve")',
      'button:has-text("Allow")',
      'button:has-text("Grant")',
      'text=/^\\s*Approve\\s*$/i',
    ]) {
      try {
        const loc = w.locator(sel).first();
        if (await loc.count() > 0 && await loc.isVisible({ timeout: 100 }).catch(() => false)) {
          return { locator: loc, selector: sel, window: w };
        }
      } catch (_) {}
    }
  }
  return null;
}
async function dumpWindows(app, tag) {
  const wins = app.windows();
  console.log(`[dump:${tag}] ${wins.length} windows`);
  for (let i = 0; i < wins.length; i++) {
    const w = wins[i];
    let url = '?', title = '?';
    try { url = w.url(); } catch (_) {}
    try { title = await w.title(); } catch (_) {}
    console.log(`  [${tag}/${i}] title=${JSON.stringify(title)} url=${url}`);
    try {
      await shot(w, `dump-${tag}-${i}`);
    } catch (_) {}
    // Dump the visible button + aria-labeled elements for selector mining
    try {
      const info = await w.evaluate(() => {
        const acc = [];
        for (const el of document.querySelectorAll('button, [role="button"], [aria-label], a')) {
          const r = el.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue;
          acc.push({
            tag: el.tagName.toLowerCase(),
            aria: el.getAttribute('aria-label') || '',
            title: el.getAttribute('title') || '',
            text: (el.innerText || '').trim().slice(0, 80),
            x: Math.round(r.x), y: Math.round(r.y),
          });
        }
        return acc.slice(0, 50);
      });
      for (const e of info) {
        if (!e.aria && !e.title && !e.text) continue;
        console.log(`    <${e.tag}> aria=${JSON.stringify(e.aria)} title=${JSON.stringify(e.title)} text=${JSON.stringify(e.text)} at(${e.x},${e.y})`);
      }
    } catch (e) {
      console.log(`    dump error: ${e.message}`);
    }
  }
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

  // Approval is a 3-step click: chatbox icon -> request entry -> Approve.
  let approvalStage = 0; // 0=need to open panel, 1=need to click request, 2=need to click Approve, 3=done
  let dumpedOnce = false;
  for (let i = 0; i < 240; i++) {
    if (approvalStage < 3) {
      if (approvalStage === 0) {
        const open = await openRightPanel(app);
        if (open) {
          log(`opening right panel via "${open.selector}" (aria="${open.aria}" title="${open.title}")`);
          await shot(open.window, `approval-stage0-${i}s`);
          try { await open.locator.click({ timeout: 3_000 }); approvalStage = 1; }
          catch (e) { log(`stage0 click failed: ${e.message}`); }
        }
      } else if (approvalStage === 1) {
        const entry = await findPermissionRequestEntry(app);
        if (entry) {
          log(`clicking permission request via "${entry.selector}"`);
          await shot(entry.window, `approval-stage1-${i}s`);
          try { await entry.locator.click({ timeout: 3_000 }); approvalStage = 2; }
          catch (e) { log(`stage1 click failed: ${e.message}`); }
        }
      } else if (approvalStage === 2) {
        const approve = await findApproveButton(app);
        if (approve) {
          log(`clicking Approve via "${approve.selector}"`);
          await shot(approve.window, `approval-stage2-${i}s`);
          try { await approve.locator.click({ timeout: 3_000 }); approvalStage = 3; }
          catch (e) { log(`stage2 click failed: ${e.message}`); }
        }
      }
      if (approvalStage < 3 && !dumpedOnce && i === 15) {
        log('--- first dump (no progress in 15s): all windows + clickable elements ---');
        await dumpWindows(app, 'wait15s');
        dumpedOnce = true;
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
