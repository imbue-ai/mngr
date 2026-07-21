// Renderer-contract regression test for the landing page's stopped-mind
// click-through fast path.
//
// Drives the REAL mithril LandingPage component against the ACTUAL rendered
// page -- no Electron app, no Docker, no live backend. The page HTML is
// produced by the real ``render_landing_page`` (shelled through ``uv``) with
// a boot island carrying one STOPPED and one RUNNING shutdown-capable mind,
// and the real compiled bundle (static/dist/chrome.bundle.js) is served from
// disk, so the mount + click path under test is byte-for-byte what ships; we
// only stub the network so the click's navigation can be observed without a
// server behind it.
//
// The contract: clicking a mind whose container the landing page already
// knows is STOPPED must route straight to the recovery page with
// ``intent=restart`` (which cold-boots the host immediately) instead of
// navigating to the normal workspace loader, where it would otherwise sit
// for the full HEALTHY->STUCK probe-failure threshold before any restart was
// dispatched. A RUNNING mind must still navigate to its normal ``/goto/``
// href.
//
// Like recovery-redirect.spec.js, this is a fast DOM-level contract test,
// not one of the heavy app-launch specs; it is run via ``pnpm test:e2e``
// locally.

const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');
const { test, expect } = require('@playwright/test');

const REPO_ROOT = path.join(__dirname, '..', '..', '..', '..');
const BUNDLE_PATH = path.join(
  __dirname, '..', '..', 'imbue', 'minds', 'desktop_client', 'static', 'dist', 'chrome.bundle.js');
const ORIGIN = 'http://localhost:8421';
// Valid AgentId shape is ``agent-`` + 32 hex chars.
const STOPPED_ID = 'agent-' + 'a'.repeat(32);
const RUNNING_ID = 'agent-' + 'b'.repeat(32);

const gotoHref = (agentId) => ORIGIN + '/goto/' + agentId + '/';
const expectedRecoveryUrl = (agentId) =>
  ORIGIN + '/agents/' + agentId + '/recovery?return_to=' +
  encodeURIComponent(gotoHref(agentId)) + '&intent=restart';

// Render the real landing page once, with one STOPPED and one RUNNING
// shutdown-capable mind in the boot island, using the production
// ``render_landing_page``. Shelling to ``uv`` keeps the island + mount under
// test identical to what ships rather than a hand-copied stub.
const PY_RENDER = `
import sys
from imbue.minds.desktop_client.chrome_state import ChromeBootState
from imbue.minds.desktop_client.chrome_state import ChromeProvidersPayload
from imbue.minds.desktop_client.chrome_state import ChromeRequestsPayload
from imbue.minds.desktop_client.chrome_state import ChromeWorkspaceEntry
from imbue.minds.desktop_client.chrome_state import ChromeWorkspacesPayload
from imbue.minds.desktop_client.chrome_state import LandingBootExtras
from imbue.minds.desktop_client.templates import render_landing_page

def entry(agent_id, liveness):
    return ChromeWorkspaceEntry(
        id=agent_id, name="ws-" + agent_id[-4:], accent="#0b292b",
        supports_shutdown="true", liveness=liveness,
    )

boot = ChromeBootState(
    workspaces=ChromeWorkspacesPayload(
        workspaces=(entry(${JSON.stringify(STOPPED_ID)}, "STOPPED"), entry(${JSON.stringify(RUNNING_ID)}, "RUNNING")),
        destroying_agent_ids=(),
        destroying_status_by_agent_id={},
        has_accounts=False,
        restorable_workspace_ids=(),
        remote_workspace_states={},
    ),
    providers=ChromeProvidersPayload(providers=(), last_event_at=None, last_full_snapshot_at=None),
    requests=ChromeRequestsPayload(count=0, request_ids=(), cards=(), auto_open=True),
    system_interface_statuses=(),
)
extras = LandingBootExtras(
    mngr_forward_origin=${JSON.stringify(ORIGIN)},
    account_email="",
    extra_account_count=0,
    locked_account_emails=(),
    is_discovering=False,
)
sys.stdout.write(render_landing_page(boot, extras))
`;

let LANDING_HTML = '';
let BUNDLE_JS = '';

test.beforeAll(() => {
  LANDING_HTML = execFileSync(
    'uv',
    ['run', '--package', 'minds', 'python', '-c', PY_RENDER],
    { cwd: REPO_ROOT, encoding: 'utf-8', maxBuffer: 16 * 1024 * 1024 },
  );
  // The compiled bundle must exist (just minds-js / pnpm run build:js).
  BUNDLE_JS = fs.readFileSync(BUNDLE_PATH, 'utf-8');
});

// Serve the rendered page for the document request and the real bundle for
// its script tag; capture (then abort) any subsequent document navigation --
// which is exactly what the component's row click performs through the
// browser host. Other sub-resources (CSS, the SSE) are aborted; the
// component under test needs none of them.
async function loadLanding(page, captured) {
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (url.endsWith('/landing')) {
      await route.fulfill({ contentType: 'text/html', body: LANDING_HTML });
    } else if (url.includes('/_static/dist/chrome.bundle.js')) {
      await route.fulfill({ contentType: 'application/javascript', body: BUNDLE_JS });
    } else if (request.isNavigationRequest()) {
      captured.push(url);
      await route.abort('aborted');
    } else {
      await route.abort('aborted');
    }
  });
  await page.goto(ORIGIN + '/landing', { waitUntil: 'domcontentloaded' });
  // Sanity: the bundle ran and the component mounted from the island.
  await expect.poll(() =>
    page.evaluate(() => document.getElementById('landing-root')?.getAttribute('data-minds-mounted')),
  ).toBe('true');
}

// Click the row's name cell (bubbles to the row's onclick) and return the URL
// the component navigated to.
async function clickRowAndCaptureNav(page, agentId, captured) {
  await page.click(`[data-agent-id="${agentId}"] .flex-1`);
  await expect.poll(() => captured.length).toBeGreaterThan(0);
  return captured[captured.length - 1];
}

test.describe('landing page stopped-mind click-through (LandingPage contract)', () => {
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
