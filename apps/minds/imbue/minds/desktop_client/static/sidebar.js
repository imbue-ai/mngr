// Electron workspace-switcher page: loaded into the shared modal
// WebContentsView when the user opens the switcher from the titlebar
// breadcrumb. Renders the floating menu (workspace list + "New workspace").
// Clicks + context menus go through window.minds IPC. In browser mode the
// chrome.js embedded menu handles the same job inline.
(function () {
  var isElectron = !!window.minds;
  // The workspace whose CONTENT is displayed (narrow: null on its settings /
  // sharing screens). ``currentScopeAgentId`` is the wider accent-source
  // workspace -- the one whose scope is active, including those screens -- and
  // is what the current-row highlight keys off so opening a mind's settings
  // still marks that mind as current.
  var currentWorkspaceId = null;
  var currentScopeAgentId = null;
  var lastWorkspaces = [];

  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``).
  // Workspace links go to the plugin, not minds.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  // The floating sidebar auto-closes after the user makes a selection
  // (workspace row, "New workspace", "Manage account(s)" / "Log in", and
  // "Open in new window"). The close happens entirely on the
  // main process side: `navigate-content` and `open-workspace-in-new-window`
  // in apps/minds/electron/main.js both call closeModal(bundle) before
  // returning, so the renderer must NOT also send a `toggle-sidebar` IPC
  // here. IPCs from a single renderer are processed FIFO; a follow-up
  // toggle would see the already-closed modal and re-open it.
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

  function renderWorkspaces(workspaces) {
    var container = document.getElementById('sidebar-workspaces');
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
        // The row markup lives in the shared builder; this view passes
        // withOpenNew:true (Electron supports multi-window) and lets the
        // parent container's flex gap own the spacing. Clicks / hover /
        // context-menu are handled by the delegated document listeners below.
        container.appendChild(
          window.mindsSidebarRow.buildRow(w, {
            isCurrent: w.id === (currentScopeAgentId || currentWorkspaceId),
            withOpenNew: true,
          }),
        );
      });
    });
  }

  function handleRowClick(target) {
    var row = target.closest('.sidebar-item');
    if (!row) return;
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    if (target.closest('[data-open-new]')) { openInNewWindow(agentId); return; }
    selectWorkspace(agentId);
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('#sidebar-new-workspace')) {
      navigate('/create');
      return;
    }
    handleRowClick(e.target);
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

  // -- Modal dismissal: backdrop click -------------------------------------
  //
  // The sidebar runs inside the shared modal WebContentsView; the body is a
  // transparent backdrop with the floating panel pinned at top-left. Clicks
  // anywhere outside the panel dismiss the modal via the same IPC the
  // inbox X button uses. Escape is handled by the main process's modal
  // before-input-event listener (see openModal in electron/main.js), so we
  // don't need a JS Escape handler here.
  function dismissModal() {
    if (isElectron && window.minds.closeModal) window.minds.closeModal();
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('#sidebar-menu')) return;
    dismissModal();
  });

  // Repaint rows when the shared backup-health cache updates so the backup
  // warning badge appears/disappears without a workspace-list event.
  if (window.mindsBackupHealth) {
    window.mindsBackupHealth.onUpdate(function () { renderWorkspaces(lastWorkspaces); });
  }

  if (isElectron && window.minds.onCurrentWorkspaceChanged) {
    window.minds.onCurrentWorkspaceChanged(function (agentId) {
      currentWorkspaceId = agentId || null;
      renderWorkspaces(lastWorkspaces);
    });
  }

  // The accent-source workspace (the active scope, including a workspace's
  // settings / sharing screens) drives which row is marked current.
  if (isElectron && window.minds.onAccentChanged) {
    window.minds.onAccentChanged(function (agentId) {
      currentScopeAgentId = agentId || null;
      renderWorkspaces(lastWorkspaces);
    });
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
