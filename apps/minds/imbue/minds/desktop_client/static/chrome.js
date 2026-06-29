// Persistent chrome (titlebar + sidebar + iframe). Shared between browser
// mode (this iframe-based layout) and Electron (where the content is its
// own WebContentsView, the sidebar page is loaded into the shared modal
// WebContentsView when opened, and window.minds exposes IPC adapters).
(function () {
  var isElectron = !!window.minds;

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

  // -- Navigation adapter ---------------------------------------------------
  function navigateContent(url) {
    if (isElectron) window.minds.navigateContent(url);
    else document.getElementById('content-frame').src = url;
  }
  function goBack() {
    if (isElectron) window.minds.contentGoBack();
    else { try { document.getElementById('content-frame').contentWindow.history.back(); } catch (e) {} }
  }
  function goForward() {
    if (isElectron) window.minds.contentGoForward();
    else { try { document.getElementById('content-frame').contentWindow.history.forward(); } catch (e) {} }
  }

  // -- Sidebar toggle -------------------------------------------------------
  //
  // The menu's position is derived from the trigger button's
  // getBoundingClientRect + a caller-chosen offset (anchor model:
  // menu.top-left = trigger.bottom-left + offset). This keeps the menu
  // visually attached to whatever opens it -- if the button moves (mac
  // traffic-light spacing, a future layout change, a different control
  // entirely), the menu follows for free without baking the trigger
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
  // Nudge 2px left of the trigger's left edge, and sit 2px below its bottom.
  var SIDEBAR_OFFSET_X = -2;
  var SIDEBAR_OFFSET_Y = 2;
  var sidebarOpen = false;
  function computeSidebarAnchor() {
    var btn = document.getElementById('sidebar-toggle');
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
      };
    });
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
  function setDisplayedWorkspaceAgentId(agentId) {
    if (currentTitleAgentId !== agentId && agentId) {
      // Agent identity changed -- clear the recovery-redirect lock so a
      // user who navigates back to a still-stuck workspace gets bounced
      // to recovery again instead of landing on the 503 page.
      delete redirectedAgents[agentId];
    }
    currentTitleAgentId = agentId || null;
    if (currentTitleAgentId) maybeRedirectToRecovery();
  }

  // -- System-interface recovery redirect -----------------------------------
  //
  // SSE pushes ``system_interface_status`` events whenever an agent transitions
  // between healthy / stuck / restarting. When the currently-displayed agent
  // goes STUCK we navigate the content view to the recovery page; the recovery
  // page's own SSE subscription redirects back to ``return_to`` once the agent
  // is healthy again. We redirect at most once per stuck episode (per agent),
  // cleared by a subsequent ``healthy`` event, so the recovery page itself
  // doesn't get clobbered on repeat STUCK transitions while the user is on it.
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
  document.getElementById('sidebar-toggle').onclick = toggleSidebar;
  document.getElementById('home-btn').onclick = function () { navigateContent('/'); };
  document.getElementById('back-btn').onclick = goBack;
  document.getElementById('forward-btn').onclick = goForward;

  if (isElectron) {
    document.getElementById('min-btn').onclick = function () { window.minds.minimize(); };
    document.getElementById('max-btn').onclick = function () { window.minds.maximize(); };
    document.getElementById('close-btn').onclick = function () { window.minds.close(); };
    document.getElementById('content-frame').style.display = 'none';
    document.getElementById('sidebar-backdrop').style.display = 'none';
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

  // -- Title + URL tracking -------------------------------------------------
  function refreshAuthStatus() {
    fetch('/auth/api/status').then(function (r) { return r.json(); }).then(updateAuthUI).catch(function () {});
  }

  if (isElectron) {
    if (window.minds.onWindowTitleChange) {
      window.minds.onWindowTitleChange(function (title) {
        document.getElementById('page-title').textContent = title || 'Minds';
      });
    } else {
      window.minds.onContentTitleChange(function (title) {
        document.getElementById('page-title').textContent = title || 'Minds';
      });
    }
    // The account row that refreshAuthStatus would update lives inside the
    // inline #sidebar-backdrop, which is display:none in Electron mode --
    // the visible copy renders inside the shared modal WebContentsView when
    // it is loaded with /_chrome/sidebar, and the sidebar.js running there
    // subscribes to its own content-url-changed IPC and re-fetches
    // /auth/api/status. So we don't subscribe to onContentURLChange here in
    // Electron mode; doing so would fire the fetch on every nav for no
    // visible effect.
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
  } else {
    setInterval(function () {
      try {
        var t = document.getElementById('content-frame').contentDocument.title;
        if (t) document.getElementById('page-title').textContent = t;
        var loc = document.getElementById('content-frame').contentWindow.location.pathname;
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
    document.getElementById('content-frame').addEventListener('load', refreshAuthStatus);
  }

  // -- Auth status (drives the in-sidebar account row) ----------------------
  //
  // Browser mode renders the floating sidebar inline (this script owns it),
  // so we also keep the "Manage account(s)" / "Log in" label up-to-date here
  // by toggling the same DOM the Electron sidebar.js uses. In Electron mode
  // the sidebar lives in its own WebContentsView with its own copy of this
  // logic; the writes below land on the inline #sidebar-account, which
  // lives inside the display:none #sidebar-backdrop and so isn't
  // user-visible. The main process drives the separate view.
  var signedIn = false;
  function updateAuthUI(data) {
    signedIn = !!(data && data.signedIn);
    var label = document.getElementById('sidebar-account-label');
    var btn = document.getElementById('sidebar-account');
    if (!label || !btn) return;
    if (signedIn) {
      label.textContent = 'Manage account(s)';
      btn.title = data.email || 'Manage accounts';
    } else {
      label.textContent = 'Log in';
      btn.title = 'Sign in to your account';
    }
  }
  refreshAuthStatus();

  // -- Sidebar action wiring (browser mode only) ----------------------------
  if (!isElectron) {
    var newWsBtn = document.getElementById('sidebar-new-workspace');
    if (newWsBtn) newWsBtn.onclick = function () { navigateContent('/create'); closeSidebar(); };
    var settingsBtn = document.getElementById('sidebar-settings');
    if (settingsBtn) settingsBtn.onclick = function () { navigateContent('/settings'); closeSidebar(); };
    var accountBtn = document.getElementById('sidebar-account');
    if (accountBtn) {
      accountBtn.onclick = function () {
        navigateContent(signedIn ? '/accounts' : '/auth/login');
        closeSidebar();
      };
    }
  }

  document.getElementById('requests-toggle').onclick = function () {
    if (isElectron) window.minds.toggleInbox();
    else navigateContent('/inbox');
  };

  // Get-help opens the help modal (report a bug). Pass the currently-displayed
  // workspace id along so the report can scope workspace context; in Electron the
  // modal is the shared overlay view, in browser mode it loads into the content frame.
  document.getElementById('help-toggle').onclick = function () {
    var aid = currentTitleAgentId || '';
    if (isElectron) {
      window.minds.toggleHelp(aid);
    } else {
      navigateContent('/help' + (aid ? '?workspace=' + encodeURIComponent(aid) : ''));
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
        // withOpenNew:false (the current row still gets its settings gear).
        // Unlike the Electron sidebar (delegated listeners) this view wires
        // the click per-row, so attach it to the built element.
        var row = window.mindsSidebarRow.buildRow(w, {
          isCurrent: w.id === currentTitleAgentId,
          withOpenNew: false,
        });
        row.addEventListener('click', function (e) {
          if (e.target.closest('[data-open-settings]')) {
            navigateContent('/workspace/' + w.id + '/settings');
            closeSidebar();
            return;
          }
          selectWorkspace(w.id);
        });
        container.appendChild(row);
      });
    });
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
          accentByAgentId[data.agent_id] = {
            accent: data.accent,
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
      }
      if (data.type === 'auth_status') updateAuthUI(data);
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
