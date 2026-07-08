// Renderer-contract regression test for the landing page's stopped-mind
// click-through fast path.
//
// Drives the REAL inline landing-page JS (the ``landingRowClick`` handler in
// templates/pages/Landing.jinja) against the ACTUAL rendered page -- no
// Electron app, no Docker, no live backend. The page HTML is produced by the
// real ``render_landing_page`` (shelled through ``uv``) so the inline script we
// exercise is byte-for-byte what ships; we only stub the network so the click's
// navigation can be observed without a server behind it.
//
// The contract: clicking a mind whose container the landing page already knows
// is STOPPED must route straight to the recovery page with ``intent=restart``
// (which cold-boots the host immediately) instead of navigating to the normal
// workspace loader, where it would otherwise sit for the full HEALTHY->STUCK
// probe-failure threshold before any restart was dispatched. A RUNNING mind
// must still navigate to its normal ``/goto/`` href.
//
// Like recovery-redirect.spec.js, this is a fast DOM-level contract test, not
// one of the heavy app-launch specs; it is run via ``pnpm test:e2e`` locally.

const path = require('path');
const { execFileSync } = require('child_process');
const { test, expect } = require('@playwright/test');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const ORIGIN = 'http://localhost:8421';
// Valid AgentId shape is ``agent-`` + 32 hex chars.
const STOPPED_ID = 'agent-' + 'a'.repeat(32);
const RUNNING_ID = 'agent-' + 'b'.repeat(32);

const gotoHref = (agentId) => ORIGIN + '/goto/' + agentId + '/';
const expectedRecoveryUrl = (agentId) =>
  ORIGIN + '/agents/' + agentId + '/recovery?return_to=' +
  encodeURIComponent(gotoHref(agentId)) + '&intent=restart';

// Render the real landing page once, with one STOPPED and one RUNNING
// shutdown-capable mind, using the production ``render_landing_page``. Shelling
// to ``uv`` keeps the inline ``landingRowClick`` under test identical to what
// ships rather than a hand-copied stub.
const PY_RENDER = `
import sys
from imbue.minds.desktop_client.templates import render_landing_page
from imbue.mngr.primitives import AgentId
stopped = AgentId(${JSON.stringify(STOPPED_ID)})
running = AgentId(${JSON.stringify(RUNNING_ID)})
sys.stdout.write(render_landing_page(
    accessible_agent_ids=(stopped, running),
    mngr_forward_origin=${JSON.stringify(ORIGIN)},
    shutdown_capable_agent_ids=(stopped, running),
    mind_liveness_by_agent_id={str(stopped): "STOPPED", str(running): "RUNNING"},
))
`;

let LANDING_HTML = '';

test.beforeAll(() => {
  LANDING_HTML = execFileSync(
    'uv',
    ['run', '--package', 'minds', 'python', '-c', PY_RENDER],
    { cwd: REPO_ROOT, encoding: 'utf-8', maxBuffer: 16 * 1024 * 1024 },
  );
});

// Serve the rendered page for the document request and capture (then abort) any
// subsequent document navigation -- which is exactly the ``window.location =``
// that ``landingRowClick`` performs. Sub-resources (CSS, the landing SSE, etc.)
// are aborted too; the inline handler under test needs none of them.
async function loadLanding(page, captured) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (url.endsWith('/landing')) {
      await route.fulfill({ contentType: 'text/html', body: LANDING_HTML });
    } else if (request.isNavigationRequest()) {
      captured.push(url);
      await route.abort('aborted');
    } else {
      await route.abort('aborted');
    }
  });
  await page.goto(ORIGIN + '/landing', { waitUntil: 'domcontentloaded' });
  // Sanity: the real inline script ran and exposed the handler under test.
  await expect.poll(() => page.evaluate(() => typeof window.landingRowClick === 'function')).toBe(true);
}

// Click the row's name cell (bubbles to the Card's onclick) and return the URL
// the handler navigated to.
async function clickRowAndCaptureNav(page, agentId, captured) {
  await page.click(`[data-agent-id="${agentId}"] .flex-1`);
  await expect.poll(() => captured.length).toBeGreaterThan(0);
  return captured[captured.length - 1];
}

test.describe('landing page stopped-mind click-through (landingRowClick contract)', () => {
  test('a STOPPED mind routes to recovery with intent=restart (immediate cold-boot)', async ({ page }) => {
    const captured = [];
    await loadLanding(page, captured);
    const nav = await clickRowAndCaptureNav(page, STOPPED_ID, captured);
    expect(nav).toBe(expectedRecoveryUrl(STOPPED_ID));
  });

  test('a RUNNING mind navigates to its normal /goto/ loader', async ({ page }) => {
    const captured = [];
    await loadLanding(page, captured);
    const nav = await clickRowAndCaptureNav(page, RUNNING_ID, captured);
    expect(nav).toBe(gotoHref(RUNNING_ID));
  });
});
