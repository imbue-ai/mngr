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
  // Each SSE ``workspaces`` payload carries a per-workspace ``accent``
  // (#rrggbb) and ``accent_fg`` (RGB triple for the contrasting titlebar
  // foreground). The chrome caches both per agent id (see
  // ``rememberWorkspaceAccents`` below) so accent application is a
  // synchronous lookup. No client-side hash or hex math.

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
  var sidebarOpen = false;
  function toggleSidebar() {
    if (isElectron) {
      window.minds.toggleSidebar();
      sidebarOpen = !sidebarOpen;
    } else {
      var panel = document.getElementById('sidebar-panel');
      sidebarOpen = !sidebarOpen;
      if (sidebarOpen) panel.classList.remove('-translate-x-full');
      else panel.classList.add('-translate-x-full');
    }
  }

  function selectWorkspace(agentId) {
    navigateContent(mngrForwardOrigin + '/goto/' + agentId + '/');
    if (isElectron) {
      sidebarOpen = false;
    } else {
      sidebarOpen = false;
      document.getElementById('sidebar-panel').classList.add('-translate-x-full');
    }
  }

  // -- Titlebar accent ------------------------------------------------------
  //
  // The titlebar background and contrasting foreground are driven by three
  // CSS variables set on the document root:
  //   --workspace-accent  the workspace's #rrggbb accent (also consumed by
  //                       sidebar spines etc.)
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
  // Per-agent {accent, accent_fg} map populated from each SSE
  // ``workspaces`` payload. ``applyTitleAccent`` reads from this cache
  // so accent application is synchronous.
  // Workspaces missing from the cache (e.g. an agentId for which no SSE
  // tick has arrived yet) leave the accent unset on this call and get
  // painted by ``renderWorkspaces`` on the next tick.
  var accentByAgentId = {};
  // Tracks the agentId whose accent the chrome *wants* painted, regardless
  // of whether the SSE cache has caught up yet. Bootstrap and the
  // ``onLastWorkspaceAgentIdChanged`` path both set this even when the SSE
  // workspaces payload hasn't arrived yet (cold start, freshly-created
  // workspace); the next ``workspaces`` tick replays the paint with the
  // now-populated cache. Independent of ``currentTitleAgentId`` so the
  // accent-only call paths (bootstrap / last-workspace IPC) can update
  // the titlebar without claiming to represent the displayed workspace.
  var lastRequestedAccentAgentId = null;
  function rememberWorkspaceAccents(workspaces) {
    if (!workspaces) return;
    workspaces.forEach(function (w) {
      if (!w || !w.id) return;
      accentByAgentId[w.id] = {
        accent: typeof w.accent === 'string' ? w.accent : null,
        fg: typeof w.accent_fg === 'string' ? w.accent_fg : null,
      };
    });
  }

  function applyTitleAccent(agentId) {
    lastRequestedAccentAgentId = agentId || null;
    if (!agentId) {
      document.documentElement.style.removeProperty('--workspace-accent');
      document.documentElement.style.removeProperty('--titlebar-bg');
      document.documentElement.style.removeProperty('--titlebar-fg');
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
    if (cached.fg) {
      document.documentElement.style.setProperty('--titlebar-fg', cached.fg);
    }
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
    window.minds.onContentURLChange(function () {
      refreshAuthStatus();
    });
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
        setDisplayedWorkspaceAgentId(derivedAgentId);
        applyTitleAccent(derivedAgentId);
      } catch (e) {}
    }, 500);
    document.getElementById('content-frame').addEventListener('load', refreshAuthStatus);
  }

  // -- Auth status ----------------------------------------------------------
  var signedIn = false;
  function updateAuthUI(data) {
    var btn = document.getElementById('user-btn');
    if (data.signedIn) {
      signedIn = true;
      btn.textContent = 'Manage account(s)';
      btn.title = data.email || 'Manage accounts';
    } else {
      signedIn = false;
      btn.textContent = 'Log in';
      btn.title = 'Sign in to your account';
    }
  }
  refreshAuthStatus();

  document.getElementById('user-btn').onclick = function () {
    if (signedIn) navigateContent('/accounts');
    else navigateContent('/auth/login');
  };

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
  function renderWorkspaces(workspaces) {
    var container = document.getElementById('sidebar-workspaces');
    container.textContent = '';
    if (!workspaces || workspaces.length === 0) {
      var empty = document.createElement('div');
      empty.className = 'px-4 py-6 text-sm text-zinc-400 text-center';
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
    keys.forEach(function (key) {
      var header = document.createElement('div');
      header.className = 'px-3 pt-2 pb-0.5 text-[11px] text-zinc-400 tracking-wider';
      header.textContent = key === 'Private' ? 'PRIVATE' : key;
      container.appendChild(header);
      groups[key].forEach(function (w) {
        var row = document.createElement('div');
        row.className = 'sidebar-item cursor-pointer text-sm font-medium text-zinc-200 rounded-md mx-1.5 my-0.5 py-2.5 pl-4 pr-3 transition-colors hover:bg-white/5';
        row.textContent = w.name || w.id;
        row.setAttribute('data-agent-id', w.id);
        // Retained-but-unverified workspace (its provider's last discovery poll
        // errored): append an amber dot. The row stays fully clickable.
        if (w.is_stale) {
          row.classList.add('is-stale');
          var staleDot = document.createElement('span');
          staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 ml-1.5 rounded-full bg-amber-400/80 align-middle';
          staleDot.title = "This workspace's provider had a discovery error; its status is unverified (still usable).";
          row.appendChild(staleDot);
        }
        if (typeof w.accent === 'string') {
          row.style.setProperty('--workspace-accent', w.accent);
        }
        row.addEventListener('click', function () { selectWorkspace(w.id); });
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
      if (data.type === 'freeform_accent_preview') {
        // Create-form preview: no workspace yet, so there's no agentId
        // to key the cache by. Paint the CSS variables directly and
        // drop the SSE replay target so a background ``workspaces``
        // tick (a liveness flip or rename in any workspace) doesn't
        // repaint the previous workspace's accent over the preview
        // while the user is still on the create form. The next
        // navigation event (``current-workspace-changed`` for the new
        // workspace on submit, or the last-workspace re-query on
        // cancel) re-establishes the regular accent path.
        if (data.accent) {
          lastRequestedAccentAgentId = null;
          document.documentElement.style.setProperty('--workspace-accent', data.accent);
          document.documentElement.style.setProperty('--titlebar-bg', data.accent);
          if (data.accent_fg) {
            document.documentElement.style.setProperty('--titlebar-fg', data.accent_fg);
          }
        }
        return;
      }
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
        // titlebar. Do not gate this on ``lastRequestedAccentAgentId``:
        // /workspace/<id>/settings doesn't fire
        // ``current-workspace-changed``, so the persisted last-workspace
        // id may point at a different workspace than the one whose
        // settings page is open.
        if (data.agent_id && data.accent) {
          accentByAgentId[data.agent_id] = {
            accent: data.accent,
            fg: typeof data.accent_fg === 'string' ? data.accent_fg : null,
          };
          applyTitleAccent(data.agent_id);
        }
        return;
      }
      if (data.type === 'workspaces') {
        rememberWorkspaceAccents(data.workspaces);
        renderWorkspaces(data.workspaces);
        // Replay the most recent ``applyTitleAccent`` call now that the
        // cache has fresh data. Catches two cases:
        //   1. Cold start: bootstrap set ``lastRequestedAccentAgentId``
        //      before any SSE tick; this tick fills the cache and paints.
        //   2. Settings-page color save: the settings POST updated the
        //      resolver snapshot which triggered this tick; the cached
        //      hex is now the newly-picked one, so the chrome repaints.
        // Independent of ``currentTitleAgentId`` because the settings
        // page (and ``lastWorkspace``-driven Home views) don't update
        // it -- the persisted last-opened workspace is what drives the
        // accent.
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
