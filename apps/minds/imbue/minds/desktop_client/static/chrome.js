// Persistent chrome (titlebar + sidebar + iframe). Shared between browser
// mode (this iframe-based layout) and Electron (where the content + sidebar
// are separate WebContentsViews and window.minds exposes IPC adapters).
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
  // Browser mode: slides the floating panel into view via translate +
  // opacity. Electron mode: defers to the main process, which toggles the
  // separate sidebar WebContentsView.
  var sidebarOpen = false;
  function showSidebarPanel() {
    var panel = document.getElementById('sidebar-panel');
    panel.classList.remove('-translate-x-[260px]', 'opacity-0');
    panel.classList.add('translate-x-0', 'opacity-100');
  }
  function hideSidebarPanel() {
    var panel = document.getElementById('sidebar-panel');
    panel.classList.add('-translate-x-[260px]', 'opacity-0');
    panel.classList.remove('translate-x-0', 'opacity-100');
  }
  function toggleSidebar() {
    if (isElectron) {
      window.minds.toggleSidebar();
      sidebarOpen = !sidebarOpen;
    } else {
      sidebarOpen = !sidebarOpen;
      if (sidebarOpen) showSidebarPanel();
      else hideSidebarPanel();
    }
  }
  function closeSidebar() {
    if (!sidebarOpen) return;
    sidebarOpen = false;
    if (!isElectron) hideSidebarPanel();
    else window.minds.toggleSidebar();
  }

  function selectWorkspace(agentId) {
    navigateContent(mngrForwardOrigin + '/goto/' + agentId + '/');
    closeSidebar();
  }

  // -- Titlebar per-project swatch ------------------------------------------
  var currentTitleAgentId = null;
  function applyTitleSwatch(agentId) {
    var swatch = document.getElementById('title-swatch');
    if (!agentId) {
      swatch.classList.add('hidden');
      document.documentElement.style.removeProperty('--workspace-accent');
      currentTitleAgentId = null;
      return;
    }
    if (currentTitleAgentId !== agentId) {
      // Agent identity changed -- clear the recovery-redirect lock so a
      // user who navigates back to a still-stuck workspace gets bounced
      // to recovery again instead of landing on the 503 page.
      delete redirectedAgents[agentId];
    }
    currentTitleAgentId = agentId;
    getAccent(agentId, function (c) {
      if (currentTitleAgentId !== agentId) return;
      document.documentElement.style.setProperty('--workspace-accent', c);
      swatch.classList.remove('hidden');
    });
    maybeRedirectToRecovery();
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
    document.getElementById('sidebar-panel').style.display = 'none';
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
    // The account row that refreshAuthStatus would update lives in the inline
    // #sidebar-panel, which is display:none in Electron mode -- the visible
    // copy is the separate sidebar WebContentsView, which polls
    // /auth/api/status itself. So we don't subscribe to onContentURLChange
    // here in Electron mode; doing so would fire the fetch on every nav for
    // no visible effect.
    // In Electron mode the current workspace is authoritative via IPC: main.js
    // tracks the active workspace per bundle (handles both /goto/<id>/ URLs and
    // post-redirect agent-<id>.localhost subdomains) and pushes it here. Deriving
    // it from the content URL alone would clobber it to null on every navigation
    // that doesn't match /goto/<id>/, which would prevent the recovery-page
    // redirect from firing for the current agent.
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      currentWorkspaceId = agentId || null;
      applyTitleSwatch(agentId || null);
    });
  } else {
    setInterval(function () {
      try {
        var t = document.getElementById('content-frame').contentDocument.title;
        if (t) document.getElementById('page-title').textContent = t;
        var loc = document.getElementById('content-frame').contentWindow.location.pathname;
        var m = loc.match(/^\/goto\/([^/]+)/);
        var aid = m ? m[1] : null;
        // Re-render the inline workspace list only when the active
        // workspace actually changes; otherwise the 500ms tick would
        // tear down and rebuild every row twice per second forever.
        // SSE-driven workspace add/remove/rename still flows through
        // handleChromeEvent -> renderWorkspaces.
        var workspaceChanged = currentWorkspaceId !== aid;
        currentWorkspaceId = aid;
        applyTitleSwatch(aid);
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
  // logic; the DOM lookup below no-ops (the inline #sidebar-account is
  // hidden) and the main process drives the separate view.
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
  var currentWorkspaceId = null;

  // The 16px stroke icon helpers live in /_static/sidebar_workspace_row.js
  // (window.mindsSidebarRow) so this file and sidebar.js share one copy of
  // the SVG path data and button markup.
  var buildSettingsBtn = window.mindsSidebarRow.buildSettingsBtn;

  function renderWorkspaces(workspaces) {
    var container = document.getElementById('sidebar-workspaces');
    if (!container) return;
    container.textContent = '';
    if (!workspaces || workspaces.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'px-2 py-2 text-xs text-zinc-400 text-center';
      empty.textContent = 'No projects';
      container.appendChild(empty);
      return;
    }
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
        var row = document.createElement('div');
        var isCurrent = w.id === currentWorkspaceId;
        row.className = 'sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer text-[13px] text-white'
          + (isCurrent ? ' is-current bg-white/15' : ' hover:bg-white/5');
        row.setAttribute('data-agent-id', w.id);
        var dot = document.createElement('span');
        dot.className = 'sidebar-dot w-2.5 h-2.5 rounded-full shrink-0';
        row.appendChild(dot);
        var label = document.createElement('span');
        label.className = 'flex-1 whitespace-nowrap overflow-hidden text-ellipsis';
        label.textContent = w.name || w.id;
        row.appendChild(label);
        if (w.is_stale) {
          row.classList.add('is-stale');
          var staleDot = document.createElement('span');
          staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-amber-400/80 shrink-0';
          staleDot.title = "This workspace's provider had a discovery error; its status is unverified (still usable).";
          row.appendChild(staleDot);
        }
        if (isCurrent) {
          row.appendChild(buildSettingsBtn(w.id));
        }
        var accent = typeof w.accent === 'string' ? w.accent : null;
        if (accent) {
          dot.style.background = accent;
          row.style.setProperty('--workspace-accent', accent);
        } else {
          getAccent(w.id, function (c) {
            dot.style.background = c;
            row.style.setProperty('--workspace-accent', c);
          });
        }
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
