// Persistent chrome (titlebar + iframe). Shared between browser mode (this
// iframe-based layout) and Electron (where the content is a separate
// WebContentsView and window.minds exposes IPC adapters).
(function () {
  var isElectron = !!window.minds;

  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``).
  // Workspace links (``/goto/<agent>/``) target the plugin, not minds.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  // -- Per-agent accent color ------------------------------------------------
  //
  // The shared `window.mindsAccent.get(agentId, cb)` helper (loaded from
  // /_static/workspace_accent.js) mirrors workspace_accent() in templates.py.
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
  document.getElementById('home-btn').onclick = function () { navigateContent('/'); };
  document.getElementById('back-btn').onclick = goBack;
  document.getElementById('forward-btn').onclick = goForward;

  if (isElectron) {
    document.getElementById('min-btn').onclick = function () { window.minds.minimize(); };
    document.getElementById('max-btn').onclick = function () { window.minds.maximize(); };
    document.getElementById('close-btn').onclick = function () { window.minds.close(); };
    document.getElementById('content-frame').style.display = 'none';
    // Electron drives the inbox via its modal WebContentsView; the
    // browser-mode iframe host is never used. Drop it from the DOM so it
    // can't be accidentally raised by a stray show().
    var inboxHost = document.getElementById('requests-inbox-host');
    if (inboxHost) inboxHost.remove();
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
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      applyTitleSwatch(agentId || null);
    });
  } else {
    setInterval(function () {
      try {
        var t = document.getElementById('content-frame').contentDocument.title;
        if (t) document.getElementById('page-title').textContent = t;
        var loc = document.getElementById('content-frame').contentWindow.location.pathname;
        var m = loc.match(/^\/goto\/([^/]+)/);
        applyTitleSwatch(m ? m[1] : null);
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

  // -- Browser-mode inbox modal host ---------------------------------------
  //
  // The inbox page is the same in Electron (loaded into a transparent
  // WebContentsView overlay) and in browser mode (loaded into an
  // iframe layered over the content). In browser mode chrome.js owns
  // showing / hiding the iframe; the inbox page drives everything else,
  // and posts ``minds:close-requests-inbox`` here on close.
  function browserInboxHost() { return document.getElementById('requests-inbox-host'); }
  function browserInboxFrame() { return document.getElementById('requests-inbox-iframe'); }

  function showBrowserInbox(eventId) {
    var host = browserInboxHost();
    var frame = browserInboxFrame();
    if (!host || !frame) return;
    var url = '/_chrome/requests-inbox';
    if (eventId) url += '?event_id=' + encodeURIComponent(eventId);
    // Always reset src so the inbox page picks up the latest event_id
    // and re-fetches the list. The page's own JS handles staleness via
    // SSE, but the URL is the canonical source for the auto-selected
    // event on open.
    frame.src = url;
    host.classList.remove('hidden');
    host.dataset.state = 'open';
  }

  function hideBrowserInbox() {
    var host = browserInboxHost();
    var frame = browserInboxFrame();
    if (!host || !frame) return;
    host.classList.add('hidden');
    host.dataset.state = 'closed';
    // Blank out the iframe so its SSE subscription, timers, and any
    // in-flight form submissions are released. Next open re-loads.
    frame.src = 'about:blank';
  }

  function isBrowserInboxOpen() {
    var host = browserInboxHost();
    return !!host && host.dataset.state === 'open';
  }

  function toggleBrowserInbox() {
    if (isBrowserInboxOpen()) hideBrowserInbox();
    else showBrowserInbox(null);
  }

  document.getElementById('requests-toggle').onclick = function () {
    if (isElectron && window.minds.toggleRequestsPanel) {
      window.minds.toggleRequestsPanel();
    } else {
      toggleBrowserInbox();
    }
  };

  // The inbox page (whether loaded in the Electron modal view or in the
  // browser iframe) posts back to its parent when the user closes the
  // modal. Electron has its own IPC-based path; the parent message is
  // browser-only.
  window.addEventListener('message', function (e) {
    var data = e.data;
    if (!data || typeof data !== 'object') return;
    var frame = browserInboxFrame();
    if (data.type === 'minds:close-requests-inbox') {
      if (!frame || e.source !== frame.contentWindow) return;
      hideBrowserInbox();
      return;
    }
    // Open-request relay from the (cross-origin) workspace content
    // iframe. In Electron this is handled by the content view's preload
    // + main process; in browser mode the chrome owns it.
    if (!isElectron && data.type === 'minds:open-request-modal') {
      var contentFrame = document.getElementById('content-frame');
      if (!contentFrame || e.source !== contentFrame.contentWindow) return;
      var requestId = data.requestId;
      if (typeof requestId !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) return;
      showBrowserInbox(requestId);
    }
  });

  function updateRequestsBadge(count) {
    var badge = document.getElementById('requests-badge');
    if (!badge) return;
    if (count > 0) badge.classList.remove('hidden');
    else badge.classList.add('hidden');
  }

  // Track the previously-seen pending request ids so browser-mode can
  // mirror Electron's auto-open / force-open behavior.
  var prevRequestIds = [];

  function handleChromeEvent(data) {
    try {
      if (data.type === 'auth_status') updateAuthUI(data);
      if (data.type === 'requests') {
        updateRequestsBadge(data.count);
        if (!isElectron) handleBrowserInboxAutoOpen(data);
      }
      if (data.type === 'system_interface_status') handleSystemInterfaceStatus(data.agent_id, data.status);
    } catch (e) {}
  }

  function handleBrowserInboxAutoOpen(data) {
    var newIds = Array.isArray(data.request_ids) ? data.request_ids.map(String) : [];
    var prevSet = new Set(prevRequestIds);
    var hasNewRequest = newIds.some(function (id) { return !prevSet.has(id); });
    var autoOpen = data.auto_open !== false;
    var forceOpenEventId = typeof data.force_open_event_id === 'string' ? data.force_open_event_id : null;
    prevRequestIds = newIds;
    if (isBrowserInboxOpen()) return;
    var shouldAutoOpen = (autoOpen && hasNewRequest) || forceOpenEventId !== null;
    if (shouldAutoOpen) showBrowserInbox(forceOpenEventId);
  }

  if (isElectron && window.minds.onChromeEvent) {
    window.minds.onChromeEvent(handleChromeEvent);
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
