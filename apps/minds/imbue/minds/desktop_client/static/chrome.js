// Persistent chrome (titlebar + sidebar + iframe). Shared between browser
// mode (this iframe-based layout) and Electron (where the content is its
// own WebContentsView, the sidebar page is loaded into the shared modal
// WebContentsView when opened, and window.minds exposes IPC adapters).
(function () {
  var isElectron = !!window.minds;

  // A trusted local/native page (Landing, Create, Settings, ...) renders its
  // own body directly under the titlebar via ChromeShell and has NO
  // #content-frame -- it IS the main frame, so navigation is full-page rather
  // than driving a child iframe (browser) or the content WebContentsView. The
  // agent-wrapper page (pages.Chrome) and the Electron chrome view both carry a
  // #content-frame; a local page does not. This holds in both browser and
  // Electron mode. A FUNCTION (not a boot-time constant) because the swap
  // engine below can exchange the page body in place -- including swapping the
  // wrapper's iframe in or out -- so the answer can change over this
  // document's lifetime.
  function isLocalPage() {
    return !document.getElementById('content-frame');
  }

  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``).
  // Workspace links (``/goto/<agent>/``) target the plugin, not minds.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  // Which workspace's accent (if any) a same-origin minds content path
  // belongs to. Recognises the workspace-scoped backend routes
  // (settings / sharing / destroying / recovery) plus ``/goto/<id>/``,
  // and returns null for every general screen so the bar paints the
  // neutral chrome there. Browser-mode mirror of
  // ``parseAccentSourceAgentId`` in electron/main.js (path-only -- the
  // poll reads ``location.pathname``; cross-origin workspace subdomains
  // throw before this is reached, which the poll's try/catch swallows).
  function accentSourceFromPath(pathname) {
    if (!pathname) return null;
    var m =
      pathname.match(/^\/(?:goto|workspace|sharing)\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      pathname.match(/^\/destroying\/(agent-[a-f0-9]+)(?:\/|$)/i) ||
      pathname.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
    return m ? m[1] : null;
  }

  // -- Per-agent accent color ------------------------------------------------
  //
  // Each SSE ``workspaces`` payload carries a per-workspace ``accent``
  // (#rrggbb). The chrome caches it per agent id (see
  // ``rememberWorkspaceAccents`` below) so accent application is a
  // synchronous lookup. The contrasting titlebar foreground is derived
  // from the accent in pure CSS (``.titlebar-surface`` in app.css), not here.

  // -- Local page swap engine ------------------------------------------------
  //
  // The chrome shell document is PERSISTENT for the hub pages: navigating
  // among them fetches the target page and swaps #local-page-root +
  // #local-page-scripts in place (then pushState), so the titlebar and shell
  // scripts never rebuild -- no white flash, no breadcrumb blink, ~instant.
  // Mirrors isSwappableLocalPath in electron/surface-routing.js (this script
  // cannot require it); keep the two lists in sync. Pages excluded from the
  // list (welcome, creating, destroying, auth, help, full-page sharing) do
  // FULL navigations so their timers / pollers / SSE subscriptions get a
  // real document lifecycle. A swapped-out page receives a
  // ``minds:page-teardown`` window event to close its own live resources;
  // swappable pages with loops (the landing list, recovery) guard them on it.
  function isSwappablePath(pathname) {
    if (!pathname) return false;
    return (
      pathname === '/'
      || pathname === '/create'
      || pathname === '/create/inspiration'
      || pathname === '/settings'
      || pathname === '/accounts'
      || pathname === '/_chrome'
      || /^\/workspace\/agent-[a-f0-9]+\/settings$/i.test(pathname)
      // Recovery is swappable: its poll loops are minds:page-teardown-guarded
      // and its card CSS lives in the page body, so hub <-> recovery hops (a
      // flapping workspace's most common transition) keep the titlebar intact.
      || /^\/agents\/agent-[a-f0-9]+\/recovery$/i.test(pathname)
    );
  }
  function canSwapTo(url) {
    // Only same-origin hub pages, only when this document carries the swap
    // containers, and -- crucially -- only when the CURRENT page is itself a
    // hub page: swapping out of an excluded page (creating, destroying,
    // welcome) would leave its auto-navigating timers/pollers alive in the
    // persistent document. Those pages always leave via a full navigation,
    // which tears their document down properly.
    if (!document.getElementById('local-page-root')) return false;
    if (!isSwappablePath(window.location.pathname)) return false;
    try {
      var u = new URL(url, window.location.href);
      return u.origin === window.location.origin && isSwappablePath(u.pathname);
    } catch (e) {
      return false;
    }
  }
  var swapSeq = 0;
  function swapLocalPage(url, opts) {
    var seq = ++swapSeq;
    return fetch(url, { credentials: 'same-origin' }).then(function (resp) {
      if (!resp.ok) throw new Error('swap fetch HTTP ' + resp.status);
      if (resp.redirected) {
        // The hub page redirected (e.g. / -> /welcome after auth expiry).
        // Swapping would install the target's content under the wrong URL
        // without a document lifecycle; hand the redirect TARGET to a real
        // navigation instead.
        if (seq === swapSeq) window.location.href = resp.url;
        return null;
      }
      return resp.text();
    }).then(function (htmlText) {
      if (htmlText === null) return;
      if (seq !== swapSeq) return; // superseded by a newer swap
      var doc = new DOMParser().parseFromString(htmlText, 'text/html');
      var newRoot = doc.getElementById('local-page-root');
      var newScripts = doc.getElementById('local-page-scripts');
      var curRoot = document.getElementById('local-page-root');
      var curScripts = document.getElementById('local-page-scripts');
      if (!newRoot || !curRoot) throw new Error('swap target not a shell page');
      // Let the outgoing page close its live resources (SSE, intervals).
      try { window.dispatchEvent(new Event('minds:page-teardown')); } catch (e) {}
      document.title = doc.title || 'Minds';
      document.documentElement.className = doc.documentElement.className;
      // Adopt the page's body classes/style but preserve the live modal-open
      // marker main toggles for drag-region handling.
      var modalOpen = document.body.classList.contains('modal-open');
      document.body.className = doc.body.className;
      if (modalOpen) document.body.classList.add('modal-open');
      document.body.style.cssText = doc.body.style.cssText;
      curRoot.replaceWith(document.adoptNode(newRoot));
      if (curScripts) curScripts.remove();
      // Scripts inserted via innerHTML/adopt don't execute: re-create each one
      // in order (src scripts awaited so page-script order is preserved).
      var container = document.createElement('div');
      container.id = 'local-page-scripts';
      container.style.display = 'none';
      document.body.appendChild(container);
      var chain = Promise.resolve();
      if (newScripts) {
        Array.prototype.slice.call(newScripts.querySelectorAll('script')).forEach(function (old) {
          chain = chain.then(function () {
            return new Promise(function (resolve) {
              var s = document.createElement('script');
              for (var i = 0; i < old.attributes.length; i++) {
                s.setAttribute(old.attributes[i].name, old.attributes[i].value);
              }
              if (old.src) {
                s.onload = resolve;
                s.onerror = resolve;
                container.appendChild(s);
              } else {
                s.textContent = old.textContent;
                container.appendChild(s);
                resolve();
              }
            });
          });
        });
      }
      return chain.then(function () {
        if (seq !== swapSeq) return;
        if (!(opts && opts.fromHistory)) {
          try { history.pushState({ mindsSwap: true }, '', url); } catch (e) {}
        }
        applyModeSetup();
        window.scrollTo(0, 0);
        var u = new URL(url, window.location.href);
        // Local pages stamp the titlebar from their own path. The WRAPPER must
        // NOT: its titlebar context belongs to the workspace the content view
        // displays (main pushes that URL at navigation-claim time), and
        // '/_chrome' itself classifies as the bare home bar -- stamping it here
        // wiped the workspace name + tabs the moment a wrapper arrived by swap,
        // with nothing to restore them on a parked-workspace reveal (no fresh
        // content commit follows). Same rule as the accent below.
        if (u.pathname !== '/_chrome') {
          lastContentUrl = u.pathname + u.search;
          applyTitlebarContext();
          applyTitleAccent(accentSourceFromPath(u.pathname));
        }
      });
    });
  }
  // Browser-mode history traversal across swapped entries.
  window.addEventListener('popstate', function () {
    var target = window.location.pathname + window.location.search;
    if (canSwapTo(target)) {
      swapLocalPage(target, { fromHistory: true }).catch(function () {
        window.location.reload();
      });
    }
  });
  // In-place refresh: re-fetch and swap the CURRENT page (no history entry)
  // instead of location.reload(). Pages whose content is server-rendered from
  // live state (the Home workspace list) dispatch this to pick up changes
  // without tearing down the persistent shell -- a full reload rebuilds the
  // titlebar, which reads as the navbar blinking out. Falls back to a real
  // reload when the current page isn't swappable.
  window.addEventListener('minds:refresh-local-page', function () {
    var here = window.location.pathname + window.location.search;
    if (canSwapTo(here)) {
      swapLocalPage(here, { fromHistory: true }).catch(function () {
        window.location.reload();
      });
    } else {
      window.location.reload();
    }
  });

  // -- Navigation adapter ---------------------------------------------------
  function navigateContent(url) {
    // Optimistically re-derive the titlebar context from the target URL so
    // the breadcrumb/tabs update without waiting for the navigation to land
    // (Electron re-pushes the authoritative URL on did-navigate; browser
    // mode has no push for cross-origin workspace URLs at all, so this is
    // also its only signal for those).
    lastContentUrl = url;
    applyTitlebarContext();
    if (isElectron) window.minds.navigateContent(url);
    else if (isLocalPage()) {
      if (canSwapTo(url)) {
        swapLocalPage(url).catch(function () { window.location = url; });
      } else {
        window.location = url;
      }
    } else {
      document.getElementById('content-frame').src = url;
    }
  }
  function goBack() {
    if (isElectron) window.minds.contentGoBack();
    else if (isLocalPage()) window.history.back();
    else { try { document.getElementById('content-frame').contentWindow.history.back(); } catch (e) {} }
  }

  // -- Workspace switcher menu ("sidebar") toggle -----------------------------
  //
  // The menu's position is derived from the trigger button's
  // getBoundingClientRect + a caller-chosen offset (anchor model:
  // menu.top-left = trigger.bottom-left + offset). This keeps the menu
  // visually attached to whatever opens it -- the trigger is the
  // breadcrumb's workspace-name button (#workspace-switcher-btn), and if
  // it moves the menu follows for free without baking the trigger
  // location into a server-side template branch.
  //
  // Browser mode: this script positions the inline #sidebar-menu via
  // style.left/style.top at toggle time, then toggles the backdrop's
  // hidden class. Electron mode: the rect + offset are sent over IPC;
  // main.js encodes them into /_chrome/sidebar's query string, the
  // server passes them to Sidebar.jinja, and the menu is positioned by
  // server-rendered inline style. Both modes share the same anchor math.
  //
  // ``sidebarOpen`` is intentionally browser-mode-only -- in Electron
  // the main process owns visibility (see toggleSidebar / openModal /
  // closeModal in electron/main.js).
  // Nudge left of the trigger's left edge so a menu row's workspace-name text
  // lines up under the breadcrumb's workspace-name text, and sit 2px below the
  // trigger's bottom. The -24 is that alignment magic number: a row's label
  // sits 30px inside the menu's left edge (panel p-1 4px + row px-2 8px + accent
  // dot w-2.5 10px + gap-2 8px), while the breadcrumb name sits only 6px inside
  // the trigger (the switcher button's p-1.5), so the menu shifts left by
  // 30 - 6 = 24 to make label-left meet name-left.
  var SIDEBAR_OFFSET_X = -24;
  var SIDEBAR_OFFSET_Y = 2;
  var sidebarOpen = false;
  function computeSidebarAnchor() {
    var btn = document.getElementById('workspace-switcher-btn');
    if (!btn) return null;
    var rect = btn.getBoundingClientRect();
    return {
      trigger: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
      offset: { x: SIDEBAR_OFFSET_X, y: SIDEBAR_OFFSET_Y },
    };
  }
  function positionInlineSidebarPanel(anchor) {
    var menu = document.getElementById('sidebar-menu');
    if (!menu || !anchor) return;
    menu.style.left = Math.round(anchor.trigger.x + anchor.offset.x) + 'px';
    menu.style.top = Math.round(anchor.trigger.y + anchor.trigger.height + anchor.offset.y) + 'px';
  }
  function showSidebarPanel() {
    positionInlineSidebarPanel(computeSidebarAnchor());
    document.getElementById('sidebar-backdrop').classList.remove('hidden');
  }
  function hideSidebarPanel() {
    document.getElementById('sidebar-backdrop').classList.add('hidden');
  }
  function toggleSidebar() {
    if (isElectron) {
      window.minds.toggleSidebar(computeSidebarAnchor());
    } else {
      sidebarOpen = !sidebarOpen;
      if (sidebarOpen) showSidebarPanel();
      else hideSidebarPanel();
    }
  }
  function closeSidebar() {
    if (isElectron) return;  // Electron sidebar.js handles its own dismissal.
    if (!sidebarOpen) return;
    sidebarOpen = false;
    hideSidebarPanel();
  }

  function selectWorkspace(agentId) {
    navigateContent(mngrForwardOrigin + '/goto/' + agentId + '/');
    closeSidebar();
  }

  // -- Titlebar accent ------------------------------------------------------
  //
  // The titlebar background is driven by two CSS variables set on the
  // document root, plus the ``.titlebar-surface`` class toggled on
  // #minds-titlebar:
  //   --workspace-accent  the workspace's #rrggbb accent (also consumed by
  //                       sidebar spines etc.)
  //   --titlebar-bg       the same color, used by the titlebar background
  // The contrasting foreground is NOT a variable -- the ``.titlebar-surface``
  // scope derives it from --titlebar-bg in pure CSS and re-bases the
  // foreground tokens on it (see app.css). Cleared back to the neutral chrome
  // (surface-primary bar via the Chrome.jinja fallback, app tokens for the
  // foreground) on any non-workspace minds screen -- so a sign-out /
  // workspace-delete / freshly-launched app, and plain navigation to Home /
  // Create / accounts, all render the neutral chrome.
  //
  // ``currentTitleAgentId`` tracks the workspace ACTUALLY DISPLAYED in this
  // window's content view -- it gates ``maybeRedirectToRecovery`` so a stuck
  // agent only redirects this window when this window is the one showing it.
  // It is intentionally separate from the ACCENT SOURCE (the persisted
  // last-opened workspace), which can differ when another window opens a
  // workspace while this one is on Home, sign-in, etc. Accent application
  // must never write to ``currentTitleAgentId`` or trigger recovery, or a
  // stuck agent in another window will hijack this window's content view.
  var currentTitleAgentId = null;
  // Whether the displayed workspace's content is actually reachable (a real
  // workspace) rather than the "Loading workspace" proxy loader that
  // mngr_forward serves at the workspace URL while the backend is unreachable.
  // Pushed by main.js over ``current-workspace-changed`` (from the content
  // view's HTTP status) so the get-help modal keeps "have an agent help"
  // disabled while a workspace is loading/stuck -- a state the health-tracker
  // ``systemInterfaceStatusByAgent`` signal doesn't cover during startup. In
  // browser mode there is no such signal (the content frame is cross-origin), so
  // it defaults to true there, leaving that mode's behavior unchanged.
  var currentWorkspaceContentReady = !isElectron;
  // Per-agent {accent} map populated from each SSE ``workspaces`` payload.
  // ``applyTitleAccent`` reads from this cache so accent application is
  // synchronous.
  // Workspaces missing from the cache (e.g. an agentId for which no SSE
  // tick has arrived yet) leave the accent unset on this call and get
  // painted by ``renderWorkspaces`` on the next tick.
  var accentByAgentId = {};
  // Tracks the agentId whose accent the chrome *wants* painted, regardless
  // of whether the SSE cache has caught up yet. The ``onAccentChanged`` path
  // (and, in browser mode, the URL poll) sets this even when the SSE
  // workspaces payload hasn't arrived yet (cold start, freshly-created
  // workspace); the next ``workspaces`` tick replays the paint with the
  // now-populated cache. Independent of ``currentTitleAgentId`` so the
  // accent path can update the titlebar without claiming to represent the
  // displayed workspace.
  var lastRequestedAccentAgentId = null;
  function rememberWorkspaceAccents(workspaces) {
    if (!workspaces) return;
    workspaces.forEach(function (w) {
      if (!w || !w.id) return;
      accentByAgentId[w.id] = {
        accent: typeof w.accent === 'string' ? w.accent : null,
        name: typeof w.name === 'string' ? w.name : null,
      };
    });
  }

  // -- Titlebar context (breadcrumb / icon-tabs / contextual back) -----------
  //
  // The left cluster's shape is a pure function of the content view's current
  // URL: a workspace-scoped screen shows the "/ workspace-name" breadcrumb
  // plus the Workspace / Workspace Settings icon-tabs (with the
  // tab for the visible screen highlighted); a non-workspace full page shows
  // a "/ page-name" crumb and, for pages that opted in, the contextual back
  // arrow; the home screen shows just the home button. Electron pushes the
  // URL over ``content-url-changed``; browser mode reads the iframe's
  // location in the 500ms poll (cross-origin workspace URLs throw there, so
  // ``navigateContent`` seeds the context optimistically for those).
  var lastContentUrl = null;
  // The workspace named in the breadcrumb (null outside workspace context).
  // Drives the switcher menu's target.
  var currentCrumbAgentId = null;

  function classifyContent(urlString) {
    var parsed;
    try {
      parsed = new URL(urlString, window.location.origin);
    } catch (e) {
      return { kind: 'home' };
    }
    var host = parsed.hostname;
    var path = parsed.pathname;
    var m = host.match(/^(agent-[a-f0-9]+)\.localhost$/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: 'workspace' };
    m = path.match(/^\/goto\/(agent-[a-f0-9]+)(?:\/|$)/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: 'workspace' };
    m = path.match(/^\/workspace\/(agent-[a-f0-9]+)(?:\/|$)/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: 'settings' };
    // Sharing is reached from workspace settings, so it gets the back arrow.
    m = path.match(/^\/sharing\/(agent-[a-f0-9]+)(?:\/|$)/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: null, showBack: true };
    m = path.match(/^\/destroying\/(agent-[a-f0-9]+)(?:\/|$)/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: null };
    m = path.match(/^\/agents\/(agent-[a-f0-9]+)\/recovery(?:\/|$)/i);
    if (m) return { kind: 'workspace', agentId: m[1], activeTab: null };
    // No back arrow on the create form: the titlebar home button is the
    // escape (back to the workspace list / welcome splash).
    if (path === '/create') return { kind: 'page', pageLabel: 'New workspace' };
    if (path === '/create/inspiration') return { kind: 'page', pageLabel: 'New workspace' };
    if (/^\/creating\//.test(path)) return { kind: 'page', pageLabel: 'New workspace' };
    // Browser-mode full-page fallbacks (Electron shows these as modals).
    if (path === '/settings') return { kind: 'page', pageLabel: 'Settings', showBack: true };
    if (path === '/accounts') return { kind: 'page', pageLabel: 'Accounts', showBack: true };
    // No back arrow on the auth pages (browser-mode fallbacks; Electron opens
    // the sign-in modal instead): the titlebar home button is the escape, and
    // the home route bounces back to the splash until an account option is
    // chosen.
    if (/^\/auth(?:\/|$)/.test(path)) return { kind: 'page', pageLabel: 'Sign in' };
    // The welcome splash is the committed first screen: the user must pick
    // sign up / log in / continue without an account, so the home button is
    // hidden (there is nowhere else to go yet).
    if (path === '/welcome') return { kind: 'welcome' };
    if (path === '/help') return { kind: 'page', pageLabel: 'Get help', showBack: true };
    return { kind: 'home' };
  }

  function updateRequestsBadge(count) {
    var badge = document.getElementById('requests-badge');
    if (!badge) return;
    if (count > 0) {
      // The badge is the Badge.jinja count pill; mirror its 99+ cap here.
      badge.textContent = count > 99 ? '99+' : String(count);
      badge.hidden = false;
    } else {
      // Hide via the native `hidden` attribute, not a `hidden` class: the pill
      // bakes in `inline-flex`, which beats the `.hidden` utility in the
      // cascade (so a `hidden` class would leave a stray "0" showing). The
      // `[hidden]` base rule is `display: none !important`, which wins.
      badge.hidden = true;
    }
  }

  function applyTitlebarContext() {
    var ctx = classifyContent(lastContentUrl || '/');
    var wsCrumb = document.getElementById('ws-crumb');
    var pageCrumb = document.getElementById('page-crumb');
    var backBtn = document.getElementById('back-btn');
    var homeBtn = document.getElementById('home-btn');
    // The welcome splash hides the home button: the user must resolve the
    // account choice (sign up / log in / continue without an account) before
    // there is anywhere else to go.
    if (homeBtn) {
      homeBtn.hidden = ctx.kind === 'welcome';
      // Selected (text-primary at rest) only on the landing page itself;
      // everywhere else it rests muted and rises to primary on hover.
      // Mirrors TitlebarButton's default/muted tones -- keep in sync.
      var homeSelected = ctx.kind === 'home';
      homeBtn.classList.toggle('text-primary', homeSelected);
      homeBtn.classList.toggle('text-secondary', !homeSelected);
      homeBtn.classList.toggle('hover:text-primary', !homeSelected);
    }
    var isWorkspace = ctx.kind === 'workspace';
    var prevCrumbAgentId = currentCrumbAgentId;
    currentCrumbAgentId = isWorkspace ? ctx.agentId : null;
    // Browser mode: keep the inline switcher's current-row highlight in sync
    // with the breadcrumb workspace (so a workspace's settings / sharing screen
    // still marks that workspace current). Electron's sidebar is a separate
    // view, primed over the accent-changed IPC. Re-render only on change.
    if (!isElectron && currentCrumbAgentId !== prevCrumbAgentId && lastWorkspaces) {
      renderWorkspaces(lastWorkspaces);
    }
    if (wsCrumb) wsCrumb.hidden = !isWorkspace;
    if (isWorkspace) {
      var cached = accentByAgentId[ctx.agentId];
      var nameEl = document.getElementById('workspace-switcher-name');
      // Never show the raw agent id in the breadcrumb. When the cache has no
      // name yet (fresh workspace before the first SSE tick), keep whatever
      // name is already displayed for this same agent; otherwise show a
      // placeholder until the 'workspaces' handler re-runs this with the
      // name.
      if (nameEl) {
        var knownName = cached && cached.name;
        if (knownName) {
          nameEl.textContent = knownName;
        } else if (nameEl.dataset.agentId !== ctx.agentId || !nameEl.textContent) {
          nameEl.textContent = '…';
        }
        nameEl.dataset.agentId = ctx.agentId;
      }
      ['workspace', 'settings'].forEach(function (tab) {
        var btn = document.getElementById('ws-tab-' + tab);
        if (!btn) return;
        var isActive = ctx.activeTab === tab;
        btn.classList.toggle('bg-fill-active', isActive);
        // Active tab reads text-primary; a resting tab is muted (secondary,
        // primary on hover). Mirrors TitlebarButton's tones -- keep in sync.
        btn.classList.toggle('text-primary', isActive);
        btn.classList.toggle('text-secondary', !isActive);
        btn.classList.toggle('hover:text-primary', !isActive);
        if (isActive) btn.setAttribute('aria-current', 'page');
        else btn.removeAttribute('aria-current');
      });
    }
    var isPage = ctx.kind === 'page';
    if (pageCrumb) pageCrumb.hidden = !isPage;
    if (isPage) {
      var crumbName = document.getElementById('page-crumb-name');
      if (crumbName) crumbName.textContent = ctx.pageLabel || '';
    }
    if (backBtn) backBtn.hidden = !ctx.showBack;
  }

  // Toggle the titlebar's self-theming scope. ``.titlebar-surface`` re-bases the
  // foreground tokens off --titlebar-bg in pure CSS (see app.css); it must be
  // present only while a workspace accent is set, so neutral chrome falls back
  // to the app's own tokens (correct in both light and dark).
  function setTitlebarSurface(on) {
    var tb = document.getElementById('minds-titlebar');
    if (tb) tb.classList.toggle('titlebar-surface', !!on);
  }

  function applyTitleAccent(agentId) {
    lastRequestedAccentAgentId = agentId || null;
    if (!agentId) {
      document.documentElement.style.removeProperty('--workspace-accent');
      document.documentElement.style.removeProperty('--titlebar-bg');
      setTitlebarSurface(false);
      return;
    }
    var cached = accentByAgentId[agentId];
    if (!cached || !cached.accent) {
      // No SSE entry for this agent yet (cold start, workspace just
      // created, etc.). Leave the bar at whatever it was; the next
      // ``workspaces`` tick will replay this call via
      // ``lastRequestedAccentAgentId`` and paint it.
      return;
    }
    document.documentElement.style.setProperty('--workspace-accent', cached.accent);
    document.documentElement.style.setProperty('--titlebar-bg', cached.accent);
    setTitlebarSurface(true);
  }
  // Update the "displayed workspace" tracker and trigger the recovery
  // redirect when warranted. Called from the displayed-workspace sources
  // (``onCurrentWorkspaceChanged`` in Electron, the URL-poll in browser mode)
  // but NOT from the accent-only call paths.
  // The last agent that was actually displayed, surviving the null gaps local
  // screens introduce (visiting a workspace's settings nulls the displayed
  // workspace, then revealing it re-sets it). The recovery-redirect lock is
  // cleared only when the user moves to a DIFFERENT workspace -- not on every
  // reveal of the same one -- otherwise a settings <-> workspace flip during a
  // stuck episode yanks the user to recovery on every single return.
  var lastDisplayedAgentId = null;
  function setDisplayedWorkspaceAgentId(agentId) {
    if (agentId && lastDisplayedAgentId !== agentId) {
      // Genuinely different workspace -- re-arm its once-per-episode
      // recovery redirect so a still-stuck workspace bounces to recovery
      // instead of landing on the 503 page.
      delete redirectedAgents[agentId];
    }
    if (agentId) lastDisplayedAgentId = agentId;
    currentTitleAgentId = agentId || null;
    if (currentTitleAgentId) maybeRedirectToRecovery();
  }

  // -- System-interface recovery redirect -----------------------------------
  //
  // SSE pushes ``system_interface_status`` events whenever an agent transitions
  // between healthy / stuck / restarting. When the currently-displayed agent
  // goes STUCK we navigate the content view to the recovery page; that page then
  // polls its own recovery route (a cheap liveness poll) and gets 302'd back to
  // ``return_to`` once the agent is healthy again. We redirect at most once per
  // stuck episode (per agent), cleared by a subsequent ``healthy`` event, so the
  // recovery page itself doesn't get clobbered on repeat STUCK transitions while
  // the user is on it.
  var systemInterfaceStatusByAgent = {};
  var redirectedAgents = {};

  function buildRecoveryUrl(agentId) {
    var returnTo = '';
    if (isElectron) {
      returnTo = mngrForwardOrigin + '/goto/' + agentId + '/';
    } else {
      try { returnTo = document.getElementById('content-frame').contentWindow.location.href; } catch (e) {}
      if (!returnTo) returnTo = mngrForwardOrigin + '/goto/' + agentId + '/';
    }
    return '/agents/' + encodeURIComponent(agentId) + '/recovery?return_to=' + encodeURIComponent(returnTo);
  }

  function maybeRedirectToRecovery() {
    var aid = currentTitleAgentId;
    if (!aid) return;
    if (systemInterfaceStatusByAgent[aid] !== 'stuck') return;
    if (redirectedAgents[aid]) return;
    redirectedAgents[aid] = true;
    navigateContent(buildRecoveryUrl(aid));
  }

  function handleSystemInterfaceStatus(agentId, status) {
    if (!agentId) return;
    if (status === 'healthy') {
      delete systemInterfaceStatusByAgent[agentId];
      delete redirectedAgents[agentId];
      return;
    }
    systemInterfaceStatusByAgent[agentId] = status;
    maybeRedirectToRecovery();
  }

  // -- Button wiring --------------------------------------------------------
  document.getElementById('workspace-switcher-btn').onclick = toggleSidebar;
  document.getElementById('home-btn').onclick = function () { navigateContent('/'); };
  document.getElementById('back-btn').onclick = goBack;
  document.getElementById('ws-tab-workspace').onclick = function () {
    if (currentCrumbAgentId) selectWorkspace(currentCrumbAgentId);
  };
  document.getElementById('ws-tab-settings').onclick = function () {
    if (currentCrumbAgentId) navigateContent('/workspace/' + currentCrumbAgentId + '/settings');
  };

  document.getElementById('requests-toggle').onclick = function () {
    // ``keep_open=1`` marks this as an intentional open of the whole inbox,
    // so resolving a request advances to the next pending one rather than
    // dismissing the window (notification-driven opens omit it and close).
    if (isElectron) window.minds.toggleInbox();
    else navigateContent('/inbox?keep_open=1');
  };

  // Electron-mode setup for the CURRENT page body: the agent-wrapper hides its
  // iframe (the content is a separate WebContentsView) and the browser-mode
  // switcher backdrop. Re-run after every swap -- a freshly swapped-in wrapper
  // carries a fresh (unhidden) iframe.
  function applyModeSetup() {
    var contentFrame = document.getElementById('content-frame');
    if (isElectron) {
      if (contentFrame) contentFrame.style.display = 'none';
      var backdrop = document.getElementById('sidebar-backdrop');
      if (backdrop) backdrop.style.display = 'none';
    } else if (contentFrame && !contentFrame.getAttribute('src')) {
      // Browser mode: the wrapper's iframe ships without src (Electron must
      // never load it -- see pages/Chrome.jinja); arm it here, at boot and
      // after every swap that brings the wrapper in.
      contentFrame.src = contentFrame.dataset.contentSrc || '/';
    }
  }

  if (isElectron) {
    document.getElementById('min-btn').onclick = function () { window.minds.minimize(); };
    document.getElementById('max-btn').onclick = function () { window.minds.maximize(); };
    document.getElementById('close-btn').onclick = function () { window.minds.close(); };
  } else {
    // Browser mode: backdrop click outside the panel + Escape close the
    // sidebar, matching the Electron sidebar's behavior.
    document.getElementById('sidebar-backdrop').addEventListener('click', function (e) {
      if (e.target.closest('#sidebar-menu')) return;
      closeSidebar();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeSidebar();
    });
  }
  // Per-mode page fixup for the BOOT document (swaps re-run it per swapped-in
  // page): Electron hides the browser-only iframe + inline switcher; browser
  // mode arms the wrapper iframe's deferred src.
  applyModeSetup();

  // Custom titlebar tooltips: the titlebar buttons carry ``data-tooltip``
  // labels and are wired by the shared /_static/tooltip_triggers.js (included
  // by Chrome.jinja), which is the same script the overlay's modal pages use.

  // -- Title + URL tracking -------------------------------------------------
  if (isLocalPage()) {
    // A trusted local page IS the chrome view/page's own document (there is no
    // #content-frame and no separate content WebContentsView). Derive the
    // titlebar breadcrumb + accent from OUR OWN location: main pushes
    // ``content-url-changed`` only for the agent content surface, and a local
    // page never displays a workspace, so the displayed-workspace /
    // recovery-redirect lock stays null. Runs identically in Electron and the
    // browser (the server may have pre-rendered the same context, in which
    // case this is a no-op repaint); swapped-in pages get the equivalent from
    // the swap engine, and (in Electron) the SSE ``workspaces`` tick replays
    // the accent once its color cache is primed (see handleChromeEvent).
    lastContentUrl = window.location.pathname;
    applyTitlebarContext();
    applyTitleAccent(accentSourceFromPath(window.location.pathname));
  }
  if (isElectron) {
    // The titlebar's breadcrumb / icon-tabs / contextual back arrow track the
    // content view's URL, which main pushes on every navigation (and replays
    // when this chrome view (re)loads, via primeViewWithCachedChromeState).
    // Subscribed regardless of the CURRENT body (local page vs wrapper): the
    // persistent shell can swap between them, and main's intended-surface
    // gating means pushes only arrive when the content surface owns the
    // titlebar.
    if (window.minds.onContentURLChange) {
      window.minds.onContentURLChange(function (url) {
        lastContentUrl = url || null;
        applyTitlebarContext();
      });
    }
    // Instant local navigation: main asks the shell to swap a hub page in
    // place instead of a full chrome-view load. Falls back to a real
    // navigation when the swap can't run (not a shell page, fetch failure).
    // The shell-ready handshake tells main this listener exists, so a swap is
    // never dispatched into a document that cannot hear it (e.g. a click
    // milliseconds after a full page load, before deferred scripts ran).
    if (window.minds.onSwapLocalPage) {
      window.minds.onSwapLocalPage(function (url) {
        if (canSwapTo(url)) {
          swapLocalPage(url).catch(function () { window.location = url; });
        } else {
          window.location = url;
        }
      });
      if (window.minds.shellReady) window.minds.shellReady();
    }
    // In Electron mode the current workspace is authoritative via IPC: main.js
    // tracks the active workspace per bundle (handles both /goto/<id>/ URLs and
    // post-redirect agent-<id>.localhost subdomains) and pushes it here. Deriving
    // it from the content URL alone would clobber it to null on every navigation
    // that doesn't match /goto/<id>/, which would prevent the recovery-page
    // redirect from firing for the current agent.
    //
    // ``onCurrentWorkspaceChanged`` is NARROW: it carries the agent id only
    // while the content view is ACTUALLY displaying that workspace, and null on
    // every other screen (including the workspace's own settings / sharing
    // screens). It drives the recovery-redirect lock ONLY -- not the accent.
    window.minds.onCurrentWorkspaceChanged(function (agentId, contentReady) {
      currentWorkspaceContentReady = !!contentReady;
      setDisplayedWorkspaceAgentId(agentId || null);
    });
    // The titlebar accent is a pure function of the current screen, pushed by
    // main on every navigation: the workspace id on any workspace-scoped screen
    // (the workspace itself plus its settings / sharing / destroying / recovery
    // screens) and null on a general screen, where the neutral chrome takes
    // over. Apply it unconditionally -- main is the single source of truth, so
    // there is nothing to remember, re-query, or gate here. Main also re-pushes
    // the current value when this chrome view (re)loads (via
    // ``primeViewWithCachedChromeState``), so a fresh / rebuilt view paints the
    // right accent without a bootstrap round-trip.
    window.minds.onAccentChanged(function (agentId) {
      applyTitleAccent(agentId || null);
    });
  } else if (!isLocalPage()) {
    setInterval(function () {
      try {
        if (isLocalPage()) return;
        var loc = document.getElementById('content-frame').contentWindow.location.pathname;
        if (lastContentUrl !== loc) {
          lastContentUrl = loc;
          applyTitlebarContext();
        }
        var m = loc.match(/^\/goto\/([^/]+)/);
        var derivedAgentId = m ? m[1] : null;
        // Re-render the inline workspace list only when the displayed
        // workspace actually changes; otherwise the 500ms tick would
        // tear down and rebuild every row twice per second forever.
        // SSE-driven workspace add/remove/rename still flows through
        // handleChromeEvent -> renderWorkspaces.
        var workspaceChanged = currentTitleAgentId !== derivedAgentId;
        setDisplayedWorkspaceAgentId(derivedAgentId);
        // The titlebar accent tracks a WIDER set than the displayed
        // workspace: the workspace-scoped minds screens (settings,
        // sharing, ...) keep the workspace's color even though they're
        // not the workspace itself, while every general screen (Home,
        // Create, accounts, ...) resolves to null and paints the neutral
        // chrome. Mirrors ``parseAccentSourceAgentId`` in electron/main.js.
        applyTitleAccent(accentSourceFromPath(loc));
        if (workspaceChanged) renderWorkspaces(lastWorkspaces);
      } catch (e) {}
    }, 500);
  }

  // Paint the initial titlebar context (home state until the first content
  // URL push / poll tick lands).
  applyTitlebarContext();

  // -- Switcher menu action wiring (browser mode only) -----------------------
  if (!isElectron) {
    var newWsBtn = document.getElementById('sidebar-new-workspace');
    if (newWsBtn) newWsBtn.onclick = function () { navigateContent('/create'); closeSidebar(); };
  }

  // The report-a-bug button opens the help modal (report a bug). Pass the currently-displayed
  // workspace id along so the report can scope workspace context; in Electron the
  // modal is the shared overlay view, in browser mode it loads into the content frame.
  document.getElementById('help-toggle').onclick = function () {
    var aid = currentTitleAgentId || '';
    // Agent-help spawns an /assist chat *inside* the displayed workspace, so it is only usable when
    // that workspace is actually reachable: on a loading/stuck workspace the new chat couldn't be
    // seen or reached (and the spawn would fail). Gate the option on BOTH signals -- a truthy
    // systemInterfaceStatusByAgent entry means stuck/restarting, and currentWorkspaceContentReady is
    // false while the content view shows the "Loading workspace" proxy loader (which the stuck signal
    // doesn't cover during startup) -- while still passing the workspace id so a bug report stays
    // scoped to it even when it's down.
    var assistAvailable = !!aid && !systemInterfaceStatusByAgent[aid] && currentWorkspaceContentReady;
    if (isElectron) {
      window.minds.toggleHelp(aid, assistAvailable);
    } else {
      var helpQuery = aid ? '?workspace=' + encodeURIComponent(aid) + (assistAvailable ? '&assist=1' : '') : '';
      navigateContent('/help' + helpQuery);
    }
  };

  // -- Open a permission request from workspace content (browser mode) -------
  //
  // The workspace (the cross-origin content iframe) can ask the shell to show
  // a permission request by posting `{type:'minds:open-request-modal',
  // requestId}` to `window.parent`. In Electron this is handled by the content
  // view's relay preload + main process (which opens the inbox modal pre-
  // selected on the target); in browser mode there is no overlay, so we
  // navigate the content iframe to the inbox page instead. Only honour
  // messages from the content iframe itself, and only well-formed server-
  // issued ids (`evt-<uuid hex>`), so arbitrary pages cannot drive navigation.
  if (!isElectron) {
    window.addEventListener('message', function (e) {
      var frame = document.getElementById('content-frame');
      if (!frame || e.source !== frame.contentWindow) return;
      var data = e.data;
      if (!data || typeof data !== 'object') return;
      if (data.type === 'minds:open-request-modal') {
        var requestId = data.requestId;
        if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
        navigateContent('/inbox?selected=' + encodeURIComponent(requestId));
        return;
      }
      // Error pages (e.g. the recovery page) ask to open the get-help / report-a-bug
      // modal. There's no overlay in browser mode, so navigate the content frame to
      // /help, scoped to the workspace when the page supplied a valid agent id.
      if (data.type === 'minds:open-help') {
        var agentId = data.agentId;
        var scoped = typeof agentId === 'string' && /^agent-[a-f0-9]{1,64}$/i.test(agentId) ? agentId : '';
        navigateContent('/help' + (scoped ? '?workspace=' + encodeURIComponent(scoped) : ''));
        return;
      }
    });
  }

  // -- SSE-driven sidebar (browser mode only) -------------------------------
  var lastWorkspaces = [];

  // Repaint rows when the shared backup-health cache updates so the backup
  // warning badge appears/disappears without a workspace-list event.
  if (window.mindsBackupHealth) {
    window.mindsBackupHealth.onUpdate(function () { renderWorkspaces(lastWorkspaces); });
  }

  function renderWorkspaces(workspaces) {
    var container = document.getElementById('sidebar-workspaces');
    if (!container) return;
    container.textContent = '';
    if (!workspaces || workspaces.length === 0) return;
    var groups = {};
    workspaces.forEach(function (w) {
      var key = w.account || 'Private';
      if (!groups[key]) groups[key] = [];
      groups[key].push(w);
    });
    var keys = Object.keys(groups).sort(function (a, b) {
      if (a === 'Private') return -1;
      if (b === 'Private') return 1;
      return a.localeCompare(b);
    });
    keys.forEach(function (key, keyIdx) {
      if (keyIdx > 0 || keys.length > 1) {
        var header = document.createElement('div');
        header.className = 'px-2 pt-2 pb-1 type-section text-tertiary';
        header.textContent = key === 'Private' ? 'Private' : key;
        container.appendChild(header);
      }
      groups[key].forEach(function (w) {
        // Shared row builder. Browser mode has no multi-window concept, so
        // no withOpenNew (rows carry no action buttons here). Unlike the
        // Electron sidebar (delegated listeners) this view wires the click
        // per-row, so attach it to the built element.
        var row = window.mindsSidebarRow.buildRow(w, {
          isCurrent: w.id === currentCrumbAgentId,
        });
        row.addEventListener('click', function () {
          // Rows for workspaces on another device are informational only.
          if (w.is_remote) return;
          selectWorkspace(w.id);
        });
        container.appendChild(row);
      });
    });
  }

  function handleChromeEvent(data) {
    try {
      if (data.type === 'workspace_accent_preview') {
        // Optimistic single-workspace cache update + repaint, emitted by
        // main.js when the settings page in this bundle picks a color.
        // Lets the chrome titlebar update instantly without waiting for
        // the POST -> mngr label -> SSE round-trip. The cross-machine
        // sync still goes through the normal SSE path; this is just a
        // local-window shortcut.
        //
        // Unconditional paint: the settings page sends this with its
        // own agent id (the workspace whose color was just picked), so
        // painting the bar for that workspace is always the right call
        // in this window. Main has already validated the agent-id +
        // hex shape and only fires this for the *sending bundle's*
        // chrome view, so a stray sender can't paint someone else's
        // titlebar. Paint unconditionally rather than gating on
        // ``lastRequestedAccentAgentId``: even though /workspace/<id>/settings
        // is itself an accent source (main already pushed this agent id over
        // ``accent-changed``), this optimistic event carries the JUST-PICKED
        // hex, which the ``accentByAgentId`` cache won't hold until the
        // settings POST -> mngr label -> SSE round-trip lands -- so we update
        // the cache entry here and repaint immediately.
        if (data.agent_id && data.accent) {
          // Update the accent WITHOUT dropping the cached name: replacing the
          // whole entry left it name-less, so the next titlebar re-render fell
          // back to the raw agent id until the following SSE tick.
          var prevEntry = accentByAgentId[data.agent_id];
          accentByAgentId[data.agent_id] = {
            accent: data.accent,
            name: (prevEntry && prevEntry.name) || null,
          };
          applyTitleAccent(data.agent_id);
        }
        return;
      }
      if (data.type === 'workspaces') {
        lastWorkspaces = data.workspaces || [];
        rememberWorkspaceAccents(lastWorkspaces);
        renderWorkspaces(lastWorkspaces);
        // Replay the most recent ``applyTitleAccent`` call now that the
        // cache has fresh data. Catches two cases:
        //   1. Cold start / freshly-created workspace: the ``accent-changed``
        //      IPC (or, in browser mode, the URL poll) set
        //      ``lastRequestedAccentAgentId`` before any SSE tick populated the
        //      cache; this tick fills the cache and paints.
        //   2. Settings-page color save: the settings POST updated the
        //      resolver snapshot which triggered this tick; the cached
        //      hex is now the newly-picked one, so the chrome repaints.
        // Independent of ``currentTitleAgentId`` because the accent source
        // (a workspace-scoped screen, which includes settings / sharing) is
        // wider than the displayed workspace -- the accent rides
        // ``lastRequestedAccentAgentId``, not the recovery-redirect lock.
        if (lastRequestedAccentAgentId) applyTitleAccent(lastRequestedAccentAgentId);
        // Re-derive the breadcrumb: the workspace name for the current crumb
        // may only now be known (cold start, rename).
        applyTitlebarContext();
      }
      if (data.type === 'requests') updateRequestsBadge(data.count);
      if (data.type === 'system_interface_status') handleSystemInterfaceStatus(data.agent_id, data.status);
    } catch (e) {}
  }

  if (isElectron && window.minds.onChromeEvent) {
    window.minds.onChromeEvent(handleChromeEvent);
    // Toggle a ``modal-open`` class on the body when the inbox modal
    // (or any modal hosted in the main process's modalView) opens or
    // closes. The chrome titlebar's CSS keys ``app-region: no-drag``
    // off this class so the OS drag region doesn't intercept clicks
    // intended for the modal's interior in the y=0..TITLEBAR strip.
    if (window.minds.onModalStateChanged) {
      window.minds.onModalStateChanged(function (data) {
        if (!data) return;
        if (data.open) document.body.classList.add('modal-open');
        else document.body.classList.remove('modal-open');
      });
    }
  } else {
    var evtSource = null;
    function connectSSE() {
      if (evtSource) evtSource.close();
      evtSource = new EventSource('/_chrome/events');
      evtSource.onmessage = function (event) {
        try { handleChromeEvent(JSON.parse(event.data)); } catch (e) {}
      };
      evtSource.onerror = function () {
        evtSource.close();
        evtSource = null;
        setTimeout(connectSSE, 5000);
      };
    }
    connectSSE();
  }
})();
