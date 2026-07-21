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
        window.MindsUI.setContentUrl(u.pathname + u.search);
        // Local pages derive their accent from their own path; the wrapper's
        // accent is owned by the Electron main pushes (and was seeded
        // server-side via the ?accent= param).
        if (u.pathname !== '/_chrome') {
          window.MindsUI.setAccentScopeAgent(accentSourceFromPath(u.pathname));
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
    // Optimistically push the target URL into the titlebar store so the
    // breadcrumb/tabs update without waiting for the navigation to land
    // (Electron re-pushes the authoritative URL on did-navigate; browser
    // mode has no push for cross-origin workspace URLs at all, so this is
    // also its only signal for those).
    window.MindsUI.setContentUrl(url);
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
  // Exported for the mithril bundle's browser host (frontend/src/host.ts):
  // converted surfaces reuse this swap-engine-aware navigation instead of
  // reimplementing it.
  window.__mindsNavigateContent = navigateContent;

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
    if (isElectron) return;  // Electron's overlay sidebar page dismisses via main's modal IPCs.
    if (!sidebarOpen) return;
    sidebarOpen = false;
    hideSidebarPanel();
  }

  // -- Displayed-workspace tracking -----------------------------------------
  //
  // Accent + breadcrumb painting live in the mithril TitleBar component and
  // its store (mounted below); this script keeps only the
  // displayed-workspace tracker that gates the recovery redirect.
  //
  // ``currentTitleAgentId`` tracks the workspace ACTUALLY DISPLAYED in this
  // window's content view -- it gates ``maybeRedirectToRecovery`` so a stuck
  // agent only redirects this window when this window is the one showing it.
  // It is intentionally separate from the ACCENT SOURCE (the persisted
  // last-opened workspace), which can differ when another window opens a
  // workspace while this one is on Home, sign-in, etc.
  var currentTitleAgentId = null;

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

  // -- Titlebar mount --------------------------------------------------------
  //
  // The titlebar interior (breadcrumb, icon-tabs, requests badge, window
  // controls) is the mithril TitleBar component, mounted once into the
  // server-rendered #minds-titlebar skeleton (a persistent shell mount --
  // the bar lives outside #local-page-root). The switcher name button calls
  // back into this script's toggle (anchor math + per-mode show).
  window.MindsUI.mountTitleBar(document.getElementById('minds-titlebar'), {
    onToggleSwitcher: toggleSidebar,
  });

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

  if (!isElectron) {
    // Browser mode: backdrop click outside the panel + Escape close the
    // sidebar, matching the Electron sidebar's behavior. (The titlebar's own
    // buttons, window controls included, are wired inside the TitleBar
    // component.)
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
    // #content-frame and no separate content WebContentsView). Seed the
    // titlebar store from OUR OWN location: main pushes
    // ``content-url-changed`` only for the agent content surface, and a local
    // page never displays a workspace, so the displayed-workspace /
    // recovery-redirect lock stays null. Runs identically in Electron and the
    // browser (the server pre-rendered the same context into the skeleton, so
    // the mounted component's first render is a no-op repaint); swapped-in
    // pages get the equivalent from the swap engine, and the SSE
    // ``workspaces`` tick replays the accent once the store's color cache is
    // primed.
    window.MindsUI.setContentUrl(window.location.pathname);
    window.MindsUI.setAccentScopeAgent(accentSourceFromPath(window.location.pathname));
  }
  if (isElectron) {
    // The titlebar component subscribes to main's content-url / accent pushes
    // itself (see mountTitleBar); this script keeps only the swap relay and
    // the displayed-workspace recovery tracking.
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
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      setDisplayedWorkspaceAgentId(agentId || null);
    });
  } else if (!isLocalPage()) {
    setInterval(function () {
      try {
        if (isLocalPage()) return;
        var loc = document.getElementById('content-frame').contentWindow.location.pathname;
        window.MindsUI.setContentUrl(loc);
        var m = loc.match(/^\/goto\/([^/]+)/);
        var derivedAgentId = m ? m[1] : null;
        setDisplayedWorkspaceAgentId(derivedAgentId);
        // The titlebar accent tracks a WIDER set than the displayed
        // workspace: the workspace-scoped minds screens (settings,
        // sharing, ...) keep the workspace's color even though they're
        // not the workspace itself, while every general screen (Home,
        // Create, accounts, ...) resolves to null and paints the neutral
        // chrome. Mirrors ``parseAccentSourceAgentId`` in electron/main.js.
        window.MindsUI.setAccentScopeAgent(accentSourceFromPath(loc));
      } catch (e) {}
    }, 500);
  }

  // -- Switcher menu mount (browser mode only) -------------------------------
  //
  // The menu interior is the mithril WorkspaceMenu component; this shell owns
  // only the toggle/anchor math and the backdrop show/hide. Mounted once at
  // shell boot (the menu lives outside #local-page-root, so hub swaps never
  // touch it); its row actions navigate through the swap-engine export and
  // dismiss via closeSidebar.
  var modalHost = null;
  if (!isElectron) {
    window.MindsUI.mountWorkspaceMenu(document.getElementById('sidebar-menu'), {
      onDismiss: closeSidebar,
    });
    // The in-document modal layer (browser-mode parity with Electron's
    // overlay view). Mounting registers it as the host adapter's openModal
    // target; the window export lets trusted local pages' inline entry
    // points (welcome / create / workspace settings) open modals without
    // the Electron bridge.
    modalHost = window.MindsUI.mountModalHost(document.getElementById('minds-modal-host'));
    window.__mindsOpenModal = function (request) { modalHost.open(request); };
  }

  // -- Open a permission request from workspace content (browser mode) -------
  //
  // The workspace (the cross-origin content iframe) can ask the shell to show
  // a permission request by posting `{type:'minds:open-request-modal',
  // requestId}` to `window.parent`. In Electron this is handled by the content
  // view's relay preload + main process (which opens the inbox modal pre-
  // selected on the target); in browser mode the in-document modal layer
  // (ModalHost, mounted above) opens the inbox the same way. Only honour
  // messages from the content iframe itself, and only well-formed server-
  // issued ids (`evt-<uuid hex>`), so arbitrary pages cannot drive the
  // modal layer.
  if (!isElectron) {
    window.addEventListener('message', function (e) {
      var frame = document.getElementById('content-frame');
      if (!frame || e.source !== frame.contentWindow) return;
      var data = e.data;
      if (!data || typeof data !== 'object') return;
      if (data.type === 'minds:open-request-modal') {
        var requestId = data.requestId;
        if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
        modalHost.open({ kind: 'inbox', selectedRequestId: requestId });
        return;
      }
      // Error pages (e.g. the recovery page) ask to open the get-help / report-a-bug
      // modal; open it over the workspace, scoped when the page supplied a
      // valid agent id.
      if (data.type === 'minds:open-help') {
        var agentId = data.agentId;
        var scoped = typeof agentId === 'string' && /^agent-[a-f0-9]{1,64}$/i.test(agentId) ? agentId : '';
        modalHost.open({ kind: 'help', workspaceAgentId: scoped || undefined });
        return;
      }
    });
  }

  // The titlebar, switcher menu and requests badge are all store-fed mithril
  // components with their own chrome-event subscription; this script's
  // handler keeps only the system-interface statuses that gate the
  // recovery-page redirect.
  function handleChromeEvent(data) {
    try {
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
