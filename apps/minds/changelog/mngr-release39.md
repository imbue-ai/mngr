Released minds 0.3.9: the app now clones the `minds-v0.3.9` default-workspace-template tag.

Fixed the launch-to-first-message e2e harness, red since 2026-07-23. Two failures, one cause: the harness still modelled a window as a single page, and the persistent-chrome-shell work split it into two surfaces.

It reported `no content page on backend origin` while the app was healthy and sitting on `/welcome`, because it matched pages against Playwright's cached `page.url`. main.js drives the window's WebContentsViews from the Electron main process (`webContents.loadURL` / `loadFile`), and those commits do not reliably reach a `connect_over_cdp` client, so Playwright can report the `shell.html` it saw at attach time forever while the view is really on `/welcome`. Page discovery now reads `location.href` from the live document.

Past that, it failed with `net::ERR_CERT_AUTHORITY_INVALID` navigating the chrome view to a workspace, because only the workspace-content session trusts the forward proxy's self-signed loopback cert (and main.js's chrome guard blocks agent URLs there anyway). The harness now keeps local pages on the chrome view and drives chat on the content view, reaching workspaces the way a user does -- letting the app route them, or clicking the tile -- instead of navigating agent URLs itself.

The workspace wait is pinned to the agent host the create returned. A window has one content view, so creating a second workspace repoints the very page the first is still showing; an unpinned wait could match the first workspace mid-handoff and check the second workspace's reply against the first one's transcript -- which already holds its own reply, so it would have passed spuriously.

The slack flow no longer fails when latchkey has never encrypted anything. `<latchkey_directory>/encryption_key` is created lazily, and a local workspace can reach the slack step without any credential having been stored yet -- seeding the mock slack creds is that first use. The harness now ensures the key via latchkey's own `load_or_create_encryption_key` (idempotent, atomic, honours the `LATCHKEY_ENCRYPTION_KEY` override) instead of demanding it already exist.

The consent screen is now dismissed wherever it appears, and the both-tiles home check asserts on the tiles. "Help improve Minds" is not once-per-run -- it can be back on a later navigation to `/`, and the run that hit it spent the rest of its time driving a dialog it believed was the home page. The check that should have caught this matched `document.body.innerText` for both host names, which the titlebar breadcrumb satisfies on any page (one host name is a prefix of the other), so it passed on the consent screen itself.

The both-tiles home check retries consent dismissal and reports the page it actually got. Consent is served on whatever the page settles into, so a single dismissal racing the load can miss it and then spend the whole budget waiting for tiles the dialog is covering. On failure it now snaps the page and includes the URL and body text, instead of a bare locator timeout that says nothing about what was on screen.

The tile checks wait for the element to be attached rather than geometrically visible. main.js collapses the chrome view to the titlebar strip while a workspace is displayed, so its home page can lay out with no usable bounding box and Playwright reports every element invisible even though the page is correct and both workspaces are listed.

The workspace-tile click is dispatched as a DOM event rather than a synthetic mouse click. The collapsed chrome view gives its page no usable bounding box, so Playwright's actionability wait can never be satisfied there. The step exercises the tile's handler -- `/goto/<agent_id>/` through the navigate-content bridge -- which the dispatched event drives exactly the same way.

The macos-launch smoke test reads page URLs from the live document too. It hit the same stale-bookkeeping failure as the Python harness -- `No content window settled on a backend URL ... observed: ["about:blank","about:blank",".../shell.html"]` -- while the app was up and on `/welcome`. It had been passing on the right side of a race rather than on correctness.

Every chrome-view click goes through one geometry-independent helper. The collapsed chrome view (a titlebar strip while a workspace is displayed) leaves its page without a usable bounding box, so Playwright's ordinary click never satisfies its actionability wait even though the element is present and correct. The workspace tile, the settings "Back to workspaces" link, and the destroy/confirm/cancel buttons all dispatch the event instead, driving the same handlers a user's click would.

The e2e leaves workspace settings through the titlebar Home crumb. It was clicking an in-page "Back to workspaces" link that WorkspaceSettings does not have -- that link lives on the app-level Settings page, and the workspace one lost it when the breadcrumb titlebar took over navigation. The test had been asserting on an affordance that no longer existed.

Navigation waits read the live document instead of `page.wait_for_url`. That API matches against Playwright's cached URL -- the same bookkeeping main-process navigations fail to update -- so it could time out on a page that had already arrived.
