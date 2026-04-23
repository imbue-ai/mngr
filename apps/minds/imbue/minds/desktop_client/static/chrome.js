// Persistent chrome (titlebar + sidebar + iframe). Shared between browser
// mode (this iframe-based layout) and Electron (where the content + sidebar
// are separate WebContentsViews and window.minds exposes IPC adapters).
(function () {
  var isElectron = !!window.minds;

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
    navigateContent('/goto/' + agentId + '/');
    if (isElectron) {
      sidebarOpen = false;
    } else {
      sidebarOpen = false;
      document.getElementById('sidebar-panel').classList.add('-translate-x-full');
    }
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
    currentTitleAgentId = agentId;
    getAccent(agentId, function (c) {
      if (currentTitleAgentId !== agentId) return;
      document.documentElement.style.setProperty('--workspace-accent', c);
      swatch.classList.remove('hidden');
    });
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
    window.minds.onContentURLChange(function (url) {
      refreshAuthStatus();
      try {
        var u = new URL(url);
        var m = u.pathname.match(/^\/goto\/([^/]+)/);
        applyTitleSwatch(m ? m[1] : null);
      } catch (e) {}
    });
    if (window.minds.onCurrentWorkspaceChanged) {
      window.minds.onCurrentWorkspaceChanged(function (agentId) {
        applyTitleSwatch(agentId || null);
      });
    }
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

  document.getElementById('requests-toggle').onclick = function () {
    if (isElectron) window.minds.toggleRequestsPanel();
  };

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
        if (typeof w.accent === 'string') {
          row.style.setProperty('--workspace-accent', w.accent);
        } else {
          getAccent(w.id, function (c) { row.style.setProperty('--workspace-accent', c); });
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
      if (data.type === 'workspaces') renderWorkspaces(data.workspaces);
      if (data.type === 'auth_status') updateAuthUI(data);
      if (data.type === 'request_count') updateRequestsBadge(data.count);
    } catch (e) {}
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
