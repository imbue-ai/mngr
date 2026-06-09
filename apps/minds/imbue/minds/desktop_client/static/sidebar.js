// Electron sidebar WebContentsView: renders the floating menu (workspace
// list + "New workspace" + "Manage account(s)"). Clicks + context menus go
// through window.minds IPC. In browser mode the chrome.js embedded sidebar
// handles the same job inline instead.
(function () {
  var isElectron = !!window.minds;
  var currentWorkspaceId = null;
  var lastWorkspaces = [];

  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``).
  // Workspace links go to the plugin, not minds.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  // Per-agent accent color comes from the shared
  // `window.mindsAccent.get(agentId, cb)` helper in
  // /_static/workspace_accent.js (itself mirroring workspace_accent() in
  // templates.py). Used only when a workspace dict arrives without an
  // `accent` field from the server.
  function getAccent(agentId, cb) { window.mindsAccent.get(agentId, cb); }

  function navigate(url) {
    if (isElectron) window.minds.navigateContent(url);
    else window.location = url;
  }

  function selectWorkspace(agentId) {
    navigate(mngrForwardOrigin + '/goto/' + agentId + '/');
  }

  function openInNewWindow(agentId) {
    if (isElectron && window.minds.openWorkspaceInNewWindow) {
      window.minds.openWorkspaceInNewWindow(agentId);
    }
  }

  function openWorkspaceSettings(agentId) {
    navigate('/workspace/' + agentId + '/settings');
  }

  // -- Per-row icon buttons -------------------------------------------------
  //
  // SVG paths for the 16px stroke icons used in workspace rows. Kept inline
  // (rather than imported from templates.py) because the page is rendered
  // once and the icons live on JS-built rows, not in JinjaX templates.
  function buildIconButton(title, pathSvg, dataAttr, agentId) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'sidebar-row-icon flex items-center justify-center bg-transparent border-none p-0.5 cursor-pointer text-white/70 rounded hover:text-white hover:bg-white/10';
    btn.title = title;
    btn.tabIndex = -1;
    btn.setAttribute(dataAttr, agentId);
    btn.innerHTML =
      '<svg class="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + pathSvg + '</svg>';
    return btn;
  }

  function buildOpenNewBtn(agentId) {
    return buildIconButton(
      'Open in new window',
      '<path d="M9.33 2.67h4v4"/><path d="M6.67 9.33L13.33 2.67"/>'
      + '<path d="M13.33 9.33v3.33a1.33 1.33 0 0 1-1.33 1.33H3.33a1.33 1.33 0 0 1-1.33-1.33V4a1.33 1.33 0 0 1 1.33-1.33h3.33"/>',
      'data-open-new',
      agentId,
    );
  }

  function buildSettingsBtn(agentId) {
    return buildIconButton(
      'Workspace settings',
      '<circle cx="8" cy="8" r="2"/>'
      + '<path d="M12.93 10a1.1 1.1 0 0 0 .22 1.21l.04.04a1.33 1.33 0 1 1-1.89 1.89l-.04-.04a1.1 1.1 0 0 0-1.21-.22 1.1 1.1 0 0 0-.67 1.01v.11a1.33 1.33 0 1 1-2.67 0v-.06A1.1 1.1 0 0 0 6 12.93a1.1 1.1 0 0 0-1.21.22l-.04.04a1.33 1.33 0 1 1-1.89-1.89l.04-.04A1.1 1.1 0 0 0 3.12 10a1.1 1.1 0 0 0-1.01-.67H2a1.33 1.33 0 1 1 0-2.67h.06A1.1 1.1 0 0 0 3.07 6a1.1 1.1 0 0 0-.22-1.21l-.04-.04a1.33 1.33 0 1 1 1.89-1.89l.04.04A1.1 1.1 0 0 0 6 3.12a1.1 1.1 0 0 0 .67-1.01V2a1.33 1.33 0 1 1 2.67 0v.06A1.1 1.1 0 0 0 10 3.07a1.1 1.1 0 0 0 1.21-.22l.04-.04a1.33 1.33 0 1 1 1.89 1.89l-.04.04A1.1 1.1 0 0 0 12.93 6a1.1 1.1 0 0 0 1.01.67H14a1.33 1.33 0 1 1 0 2.67h-.06a1.1 1.1 0 0 0-1.01.67z"/>',
      'data-open-settings',
      agentId,
    );
  }

  function renderWorkspaces(workspaces) {
    var container = document.getElementById('sidebar-workspaces');
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
        // Retained-but-unverified workspace (its provider's last discovery poll
        // errored): show an amber dot. The row stays fully clickable.
        if (w.is_stale) {
          row.classList.add('is-stale');
          var staleDot = document.createElement('span');
          staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-amber-400/80 shrink-0';
          staleDot.title = "This workspace's provider had a discovery error; its status is unverified (still usable).";
          row.appendChild(staleDot);
        }
        // Open-in-new icon. Always present in DOM but hidden by default;
        // shown on hover (and always for the current workspace, alongside
        // the settings icon, matching the Figma "selected row" treatment).
        var openBtn = buildOpenNewBtn(w.id);
        openBtn.classList.add('hidden');
        row.appendChild(openBtn);
        // Per-workspace settings icon: only the current workspace shows it
        // (Figma: selected row carries the gear). Other rows reveal just the
        // open-in-new affordance on hover.
        if (isCurrent) {
          var settingsBtn = buildSettingsBtn(w.id);
          openBtn.classList.remove('hidden');
          openBtn.classList.add('inline-flex');
          row.appendChild(settingsBtn);
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
        container.appendChild(row);
      });
    });
  }

  function handleRowClick(target) {
    var row = target.closest('.sidebar-item');
    if (!row) return;
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    if (target.closest('[data-open-new]')) { openInNewWindow(agentId); return; }
    if (target.closest('[data-open-settings]')) { openWorkspaceSettings(agentId); return; }
    selectWorkspace(agentId);
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('#sidebar-new-workspace')) {
      navigate('/create');
      return;
    }
    if (e.target.closest('#sidebar-account')) {
      navigate(signedIn ? '/accounts' : '/auth/login');
      return;
    }
    handleRowClick(e.target);
  });

  // Flip the hover affordance on the open-in-new button for non-current
  // rows. The current row already shows the icons (set in renderWorkspaces),
  // so no hover toggle is needed for it.
  document.addEventListener('mouseover', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row || row.classList.contains('is-current')) return;
    var btn = row.querySelector('.sidebar-row-icon[data-open-new]');
    if (btn) { btn.classList.remove('hidden'); btn.classList.add('inline-flex'); }
  });
  document.addEventListener('mouseout', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row || row.classList.contains('is-current')) return;
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    var btn = row.querySelector('.sidebar-row-icon[data-open-new]');
    if (btn) { btn.classList.add('hidden'); btn.classList.remove('inline-flex'); }
  });

  document.addEventListener('contextmenu', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row) return;
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    e.preventDefault();
    if (isElectron && window.minds.showWorkspaceContextMenu) {
      window.minds.showWorkspaceContextMenu(agentId, e.clientX, e.clientY);
    }
  });

  if (isElectron && window.minds.onCurrentWorkspaceChanged) {
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      currentWorkspaceId = agentId || null;
      renderWorkspaces(lastWorkspaces);
    });
  }

  // -- Auth status ----------------------------------------------------------
  //
  // The /_chrome/events SSE stream pushes workspace updates but not auth
  // transitions, so we poll /auth/api/status on load (and whenever the
  // workspace content URL changes, since a sign-in / sign-out happens in
  // that view). Mirrors chrome.js's behavior for the browser-mode chrome.
  var signedIn = false;
  function updateAccountUI(data) {
    var label = document.getElementById('sidebar-account-label');
    var btn = document.getElementById('sidebar-account');
    if (!label || !btn) return;
    if (data && data.signedIn) {
      signedIn = true;
      label.textContent = 'Manage account(s)';
      btn.title = data.email || 'Manage accounts';
    } else {
      signedIn = false;
      label.textContent = 'Log in';
      btn.title = 'Sign in to your account';
    }
  }
  function refreshAuthStatus() {
    fetch('/auth/api/status')
      .then(function (r) { return r.json(); })
      .then(updateAccountUI)
      .catch(function () {});
  }
  refreshAuthStatus();
  if (isElectron && window.minds.onContentURLChange) {
    window.minds.onContentURLChange(refreshAuthStatus);
  }

  function handleChromeEvent(data) {
    if (data.type !== 'workspaces') return;
    lastWorkspaces = data.workspaces || [];
    renderWorkspaces(lastWorkspaces);
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
