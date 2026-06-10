// Persistent chrome (titlebar + sidebar + iframe). Shared between browser
// mode (this iframe-based layout) and Electron (where the content is its
// own WebContentsView, the sidebar page is loaded into the shared modal
// WebContentsView when opened, and window.minds exposes IPC adapters).
(function () {
  var isElectron = !!window.minds;

  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``).
  // Workspace links (``/goto/<agent>/``) target the plugin, not minds.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  // -- Per-agent accent color ------------------------------------------------
  //
  // The shared `window.mindsAccent.get(agentId, cb)` helper (loaded from
  // /_static/workspace_accent.js) mirrors workspace_accent() in templates.py.
  // The server also attaches `accent` to each workspace dict over SSE so the
  // client doesn't need to compute in the common case.
  function getAccent(agentId, cb) { window.mindsAccent.get(agentId, cb); }

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
  var SIDEBAR_OFFSET_X = 0;
  // 4px gap below the trigger button's bottom edge.
  var SIDEBAR_OFFSET_Y = 4;
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
  // The titlebar background and contrasting foreground are driven by three
  // CSS variables set on the document root:
  //   --workspace-accent  the OKLCH color (also consumed by sidebar spines etc.)
  //   --titlebar-bg       the same color, used by the titlebar background
  //   --titlebar-fg       an RGB triple ("0 0 0" | "255 255 255") for the
  //                       contrasting foreground; titlebar-* utility classes
  //                       compose this with per-element alpha for hierarchy
  // Cleared back to defaults (dark bar, white foreground) when there's no
  // active workspace, so a sign-out / workspace-delete / freshly-launched
  // app renders the default zinc-900 chrome.
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
  // Tracks the in-flight accent target so the async ``getAccent`` callback
  // can guard against landing after a newer accent application has already
  // been kicked off. (``pickForeground`` is synchronous and applied inline,
  // so it doesn't need a token.) Independent of ``currentTitleAgentId`` so
  // the accent-only call paths (bootstrap + ``onLastWorkspaceAgentIdChanged``)
  // can apply colors without claiming to represent the displayed workspace.
  var pendingAccentAgentId = null;
  function applyTitleAccent(agentId) {
    if (!agentId) {
      pendingAccentAgentId = null;
      document.documentElement.style.removeProperty('--workspace-accent');
      document.documentElement.style.removeProperty('--titlebar-bg');
      document.documentElement.style.removeProperty('--titlebar-fg');
      return;
    }
    pendingAccentAgentId = agentId;
    // ``pickForeground`` is synchronous (the threshold depends only on the
    // accent's lightness, which is constant for today's hash-derived
    // accents), so we apply ``--titlebar-fg`` immediately. The background
    // color resolves asynchronously via SHA-256; the in-flight token
    // (``pendingAccentAgentId``) guards against a stale callback landing
    // after a newer ``applyTitleAccent`` has been kicked off.
    document.documentElement.style.setProperty(
      '--titlebar-fg',
      window.mindsAccent.pickForeground(),
    );
    getAccent(agentId, function (c) {
      if (pendingAccentAgentId !== agentId) return;
      document.documentElement.style.setProperty('--workspace-accent', c);
      document.documentElement.style.setProperty('--titlebar-bg', c);
    });
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
    // Distinct from the persisted "last opened workspace" accent (below):
    // ``onCurrentWorkspaceChanged`` carries null whenever the content view is
    // on a non-workspace URL (Home, sign-in, ...) so it can't be used as the
    // titlebar accent source. We track both -- the current workspace drives
    // the recovery-page redirect lock, the last-opened workspace drives the
    // accent color.
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      // Authoritative for what THIS window is displaying: drive both the
      // recovery-redirect lock and the accent off the same event.
      setDisplayedWorkspaceAgentId(agentId || null);
      if (agentId) {
        // Real workspace navigation -- apply the accent immediately. Main
        // also persists this id so it survives a restart; we don't need to
        // push it back over IPC here.
        applyTitleAccent(agentId);
        return;
      }
      // Non-workspace URL (Home, sign-in, accounts, ...): the bar should
      // track the persisted last-opened workspace, which main may have
      // *already cleared* by the time we get here (sign-out, deletion of
      // the displayed workspace). Re-query rather than relying on the
      // ``onLastWorkspaceAgentIdChanged`` broadcast: that broadcast's
      // gate (``if (currentTitleAgentId) return;``) blocks the clear in
      // any flow where the broadcast arrives BEFORE this null
      // ``current-workspace-changed``, which is the case on sign-out (the
      // two events come from different async streams and aren't ordered).
      // The deletion path explicitly orders the IPC, but pulling the
      // stored value here covers both paths uniformly.
      window.minds.getLastWorkspaceAgentId().then(function (storedId) {
        // A subsequent workspace open may have set ``currentTitleAgentId``
        // while this IPC was in flight; let that win.
        if (currentTitleAgentId) return;
        applyTitleAccent(storedId || null);
      });
    });
    // Bootstrap: paint the accent on chrome page load using the persisted
    // last-opened workspace, before any other IPC fires.
    window.minds.getLastWorkspaceAgentId().then(function (agentId) {
      if (agentId && !currentTitleAgentId) applyTitleAccent(agentId);
    });
    // Main pushes the new value on workspace-delete / sign-out / any other
    // update so the bar tracks the source of truth even when this renderer
    // wasn't the one that triggered the change.
    //
    // Scope: ``updateBundleLastWorkspaceAgentId`` in main sends this event
    // only to THIS window's chrome view, so it never carries another
    // window's state. The gate on ``currentTitleAgentId`` exists for a
    // different reason: when the displayed workspace is deleted or the
    // user signs out, main fires this with ``null`` *before* the
    // content view's redirect to ``/`` has had a chance to emit
    // ``current-workspace-changed: null``. The gate keeps the accent
    // visible across that brief window so the bar doesn't flash to the
    // default zinc-900 before the proper ``current-workspace-changed:
    // null`` branch above re-queries ``getLastWorkspaceAgentId`` and
    // applies the (now-null) value cleanly.
    window.minds.onLastWorkspaceAgentIdChanged(function (agentId) {
      if (currentTitleAgentId) return;
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
        applyTitleAccent(derivedAgentId);
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
      if (data.type !== 'minds:open-request-modal') return;
      var requestId = data.requestId;
      if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
      navigateContent('/inbox?selected=' + encodeURIComponent(requestId));
    });
  }

  // -- SSE-driven sidebar (browser mode only) -------------------------------
  var lastWorkspaces = [];

  // Reveal a non-current row's settings gear on hover (browser mode has no
  // open-in-new button). Shared handler so the inline menu matches the
  // Electron menu's hover behavior.
  window.mindsSidebarRow.wireHoverReveal();

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
        header.className = 'px-2 pt-2 pb-1 text-[10px] text-white/40 uppercase tracking-wider';
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
    if (count > 0) badge.classList.remove('hidden');
    else badge.classList.add('hidden');
  }

  function handleChromeEvent(data) {
    try {
      if (data.type === 'workspaces') {
        lastWorkspaces = data.workspaces || [];
        renderWorkspaces(lastWorkspaces);
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
