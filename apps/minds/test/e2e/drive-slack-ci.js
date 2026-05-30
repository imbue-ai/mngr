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
// Concrete observations from CI dump (run 26692570315):
//   - Window 1 (Minds chrome shell at /_chrome) has button title="Requests"
//   - Clicking it spawns Window 2 at /_chrome/requests-panel
//   - Permission entries and Approve live inside the requests-panel window
async function openRightPanel(app) {
  for (const w of app.windows()) {
    const loc = w.locator('button[title="Requests"]').first();
    try {
      if (await loc.count() > 0 && await loc.isVisible({ timeout: 100 }).catch(() => false)) {
        return { locator: loc, selector: 'button[title="Requests"]', window: w };
      }
    } catch (_) {}
  }
  return null;
}
// Find the requests-panel window (created after openRightPanel click).
function findRequestsPanelWindow(app) {
  return app.windows().find(w => {
    try { return w.url().includes('/_chrome/requests-panel'); } catch (_) { return false; }
  });
}
async function findPermissionRequestEntry(app) {
  const w = findRequestsPanelWindow(app);
  if (!w) return null;
  // Try specific selectors first, then generic clickables.
  for (const sel of [
    'text=/slack/i',
    'text=/read.?only/i',
    'text=/permission/i',
    '[role="listitem"]',
    'li',
    'button:has-text("Slack")',
    '[data-testid*="request" i]',
    // Generic: any clickable item in the panel body.
    'button',
    '[role="button"]',
    'div[onclick], div[role="button"]',
  ]) {
    try {
      const loc = w.locator(sel).first();
      if (await loc.count() > 0 && await loc.isVisible({ timeout: 100 }).catch(() => false)) {
        const text = (await loc.innerText().catch(() => '')).trim();
        // Skip clearly-irrelevant generic buttons (close, back, etc.)
        if (/^(close|back|forward|home|projects|sign in|log in|requests|cancel)$/i.test(text)) continue;
        if (text.length === 0 && sel === 'button') continue; // skip empty icon-only buttons
        return { locator: loc, selector: sel, window: w, text };
      }
    } catch (_) {}
  }
  return null;
}
async function findApproveButton(app) {
  // Approve lives in the per-request detail window opened by clicking
  // the request entry: /requests/<id>, title="Permission request: ...".
  // Fall back to scanning all windows in case the layout changes.
  const windows = app.windows();
  const ordered = [
    ...windows.filter(w => { try { return /\/requests\/[a-f0-9-]+/.test(w.url()); } catch (_) { return false; } }),
    ...windows,
  ];
  for (const w of ordered) {
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
    // Dump the body innerText so we can see empty-state, error, etc.
    try {
      const body = await w.evaluate(() => (document.body && document.body.innerText || '').slice(0, 800));
      console.log(`    body: ${JSON.stringify(body)}`);
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
  await shot(win, '05-electron-launched');
  const origin = await win.evaluate(() => location.origin);

  // The relaunched minds.app may have lost the session that
  // first-message-verify established (Cookies persistence varies with
  // when kill landed in shutdown sequence). Mint a fresh one-time code
  // and authenticate explicitly so the chrome shell renders with the
  // Requests button.
  const ONE_TIME_CODES = path.join(process.env.HOME, '.minds', 'auth', 'one_time_codes.json');
  let codes = [];
  try { codes = JSON.parse(fs.readFileSync(ONE_TIME_CODES, 'utf8')); } catch (_) {}
  const crypto = require('crypto');
  const fresh = crypto.randomBytes(32).toString('base64url');
  codes.push({ code: fresh, status: 'VALID' });
  fs.mkdirSync(path.dirname(ONE_TIME_CODES), { recursive: true });
  fs.writeFileSync(ONE_TIME_CODES, JSON.stringify(codes, null, 2));
  log(`minted fresh one-time code (head ${fresh.slice(0, 12)})`);
  await win.goto(origin + '/authenticate?one_time_code=' + fresh);
  log(`auth navigated; final URL=${win.url()}`);
  await shot(win, '06-after-auth');

  // Now the home page should show the workspace tile.
  await win.goto(origin + '/');
  await win.waitForSelector(`text=${WORKSPACE}`, { timeout: 60_000 });
  await shot(win, '07-home-with-workspace-tile');
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
  // Snapshot the chat panel BEFORE sending the slack prompt -- the
  // prior "pong" reply from first-message-verify should be visible
  // here, which proves the launch + agent-create + first-message
  // round trip worked end-to-end.
  await shot(win, '07a-chat-with-first-message-reply');
  const beforeText = await win.evaluate(() => document.body.innerText.toLowerCase());
  const oldCannedOcc = beforeText.split(CANNED_BODY_LC).length - 1;
  log(`pre-send canned-body occurrences: ${oldCannedOcc}`);

  await input.fill(PROMPT);
  await input.press('Enter');
  await shot(win, '08-slack-prompt-sent');
  log('typed + sent; watching for approval UI and canned body');

  // Approval is up to 3 clicks: chatbox icon (if panel not open) ->
  // request entry -> Approve.
  // Wait for the agent to emit its "requested permission" signal in
  // the chat panel before opening the requests-panel. Opening it
  // before the gateway has stored the request leaves the panel
  // showing "Requests (0)" with no re-render when the entry lands.
  let approvalStage = 0;
  let waitingForAgentRequest = true;
  for (let i = 0; i < 240; i++) {
    if (approvalStage < 3) {
      // Gate stage 0 on the agent having actually requested permission
      // in chat. Opening the panel before the request reaches the
      // gateway storage shows "Requests (0)" with no re-render.
      if (waitingForAgentRequest) {
        const chatBody = await win.evaluate(() => document.body.innerText).catch(() => '');
        // Catch the many phrasings the agent uses for "I've made the
        // request, waiting for you" -- covers "requested read permission",
        // "permission request submitted", "waiting for approval", etc.
        // Plus a structural signal: if the requests-panel WINDOW exists
        // already, the request must have landed (the panel auto-opens
        // on incoming).
        const panelOpen = !!findRequestsPanelWindow(app);
        if (panelOpen
            || /requested.*slack|slack permission|permission request|wait.*approval|awaiting.*approval|approval.*proceed/i.test(chatBody)) {
          log(`agent emitted permission request (panelOpen=${panelOpen}); opening panel now`);
          waitingForAgentRequest = false;
          // Small wait so the gateway has time to persist the request
          // entry before the panel mounts.
          await win.waitForTimeout(2000);
        }
      }
      if (waitingForAgentRequest) {
        // Don't try to click yet; the panel would just be empty.
      } else {
      // If the requests-panel auto-opened (sometimes minds.app opens
      // it when a request lands), skip stage 0.
      if (approvalStage === 0 && findRequestsPanelWindow(app)) {
        log(`requests-panel auto-opened; jumping to stage 1`);
        approvalStage = 1;
      }
      if (approvalStage === 0) {
        const open = await openRightPanel(app);
        if (open) {
          log(`opening right panel via "${open.selector}"`);
          await shot(open.window, `09-stage0-requests-button-clicked-t${i}s`);
          try { await open.locator.click({ timeout: 3_000 }); approvalStage = 1; }
          catch (e) { log(`stage0 click failed: ${e.message}`); }
        }
      } else if (approvalStage === 1) {
        const entry = await findPermissionRequestEntry(app);
        if (entry) {
          log(`clicking permission request via "${entry.selector}"`);
          await shot(entry.window, `10-stage1-permission-entry-clicked-t${i}s`);
          try { await entry.locator.click({ timeout: 3_000 }); approvalStage = 2; }
          catch (e) { log(`stage1 click failed: ${e.message}`); }
        }
      } else if (approvalStage === 2) {
        const approve = await findApproveButton(app);
        if (approve) {
          log(`clicking Approve via "${approve.selector}"`);
          await shot(approve.window, `11-stage2-approve-clicked-t${i}s`);
          try {
            await approve.locator.click({ timeout: 3_000 });
            approvalStage = 3;
            // Click lands and the gateway grants the permission, but
            // claude won't retry the gated tool call on its own --
            // it's still parked on "waiting for approval". Send a kick
            // to make it retry.
            await win.waitForTimeout(2000);
            try {
              await input.fill('Permission approved. Please retry the read-only Slack read now and respond with the prefix "TOK ' + NONCE + ':" followed by the message text.');
              await input.press('Enter');
              log('sent post-approval kick to agent');
            } catch (e) {
              log(`post-approval kick failed: ${e.message}`);
            }
          } catch (e) { log(`stage2 click failed: ${e.message}`); }
        }
      }
      } // close `else (waitingForAgentRequest)`
      // Dump windows + clickable inventory periodically per stage so we
      // see how the requests-panel evolves (entries appear when the
      // agent's permission request lands -- can take 20-60s after the
      // tool call fires).
      if (approvalStage < 3 && i > 0 && i % 30 === 0) {
        const tag = `stage${approvalStage}-t${i}s`;
        log(`--- dump: stuck on stage ${approvalStage} at ${i}s ---`);
        await dumpWindows(app, tag);
      }
    }

    const body = await win.evaluate(() => document.body.innerText.toLowerCase());
    const newCannedOcc = body.split(CANNED_BODY_LC).length - 1;
    if (newCannedOcc > oldCannedOcc) {
      log(`PASS: canned body appeared at t=${i}s (occ ${oldCannedOcc} -> ${newCannedOcc})`);
      await shot(win, '12-PASS-canned-body-in-reply');
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
      log(`waiting (${i}s) approvalStage=${approvalStage} tail: ${JSON.stringify(tail.slice(-200))}`);
      // Suffix with stage so artifact list shows progress mid-flow.
      await shot(win, `13-waiting-stage${approvalStage}-t${String(i).padStart(3, '0')}s`);
    }
    await win.waitForTimeout(1000);
  }
  log('TIMEOUT (4 min)');
  await shot(win, '99-TIMEOUT-no-canned-body');
  await app.close();
  process.exit(3);
})().catch(e => {
  console.error(`[fatal] ${e.message}\n${e.stack}`);
  process.exit(99);
});
