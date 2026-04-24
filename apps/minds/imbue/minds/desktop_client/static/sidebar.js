// Electron sidebar WebContentsView: renders the workspace list and wires
// clicks + context menus through window.minds IPC. In browser mode the
// chrome.js embedded sidebar handles the same job instead.
//
// Per-agent health state (driven by the /_chrome/events workspace_server_status
// SSE stream) dims stuck / restarting rows and annotates them inline. The
// per-agent overlay (shown on top of the workspace view) is the primary
// recovery affordance; the sidebar just mirrors the state so the user can
// see at a glance which minds are healthy from the workspace list.
(function () {
  var isElectron = !!window.minds;
  var currentWorkspaceId = null;
  var lastWorkspaces = [];
  // Per-agent health map, mirrored from the SSE workspace_server_status
  // payload. Absent keys imply "healthy" -- the backend omits healthy agents
  // to keep the payload small.
  var healthByAgentId = Object.create(null);

  // Per-agent accent color comes from the shared
  // `window.mindsAccent.get(agentId, cb)` helper in
  // /_static/workspace_accent.js (itself mirroring workspace_accent() in
  // templates.py). Used only when a workspace dict arrives without an
  // `accent` field from the server.
  function getAccent(agentId, cb) { window.mindsAccent.get(agentId, cb); }

  function selectWorkspace(agentId) {
    if (isElectron) window.minds.navigateContent('/goto/' + agentId + '/');
  }

  function openInNewWindow(agentId) {
    if (isElectron && window.minds.openWorkspaceInNewWindow) {
      window.minds.openWorkspaceInNewWindow(agentId);
    }
  }

  function buildOpenNewBtn(agentId) {
    var btn = document.createElement('button');
    btn.className = 'sidebar-open-new hidden items-center justify-center bg-transparent border-none p-1 cursor-pointer text-zinc-400 rounded hover:text-zinc-200 hover:bg-white/5';
    btn.title = 'Open in new window';
    btn.tabIndex = -1;
    btn.setAttribute('data-open-new', agentId);
    btn.innerHTML =
      '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M14 3h7v7"/><path d="M10 14L21 3"/>' +
      '<path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>';
    return btn;
  }

  function applyHealthToRow(row, health) {
    // health: undefined | 'STUCK' | 'RESTARTING' (healthy rows get no
    // indicator, just the default styling).
    var statusEl = row.querySelector('.sidebar-health-status');
    row.classList.toggle('opacity-60', health === 'STUCK' || health === 'RESTARTING');
    if (!statusEl) return;
    if (health === 'STUCK') {
      statusEl.textContent = 'not responding';
      statusEl.className = 'sidebar-health-status text-[11px] text-amber-400 ml-2';
      statusEl.hidden = false;
    } else if (health === 'RESTARTING') {
      statusEl.textContent = 'restarting...';
      statusEl.className = 'sidebar-health-status text-[11px] text-zinc-400 ml-2';
      statusEl.hidden = false;
    } else {
      statusEl.hidden = true;
    }
  }

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
        var isCurrent = w.id === currentWorkspaceId;
        row.className = 'sidebar-item group cursor-pointer text-sm font-medium text-zinc-200 rounded-md mx-1.5 my-0.5 py-2.5 pl-4 pr-9 flex items-center justify-between gap-2 transition-colors hover:bg-white/5'
          + (isCurrent ? ' is-current bg-white/5' : '');
        row.setAttribute('data-agent-id', w.id);
        var label = document.createElement('span');
        label.className = 'flex-1 whitespace-nowrap overflow-hidden text-ellipsis flex items-center';
        var nameSpan = document.createElement('span');
        nameSpan.className = 'truncate';
        nameSpan.textContent = w.name || w.id;
        label.appendChild(nameSpan);
        var statusSpan = document.createElement('span');
        statusSpan.className = 'sidebar-health-status text-[11px] text-amber-400 ml-2';
        statusSpan.hidden = true;
        label.appendChild(statusSpan);
        row.appendChild(label);
        var btn = buildOpenNewBtn(w.id);
        // Show the "open in new window" icon on hover (or focus-within).
        // Tailwind's `group` class on the row + `group-hover:inline-flex`
        // can't flip to inline-flex from hidden directly, so we toggle it
        // via a tiny delegated handler below.
        row.appendChild(btn);
        if (typeof w.accent === 'string') {
          row.style.setProperty('--workspace-accent', w.accent);
        } else {
          getAccent(w.id, function (c) { row.style.setProperty('--workspace-accent', c); });
        }
        applyHealthToRow(row, healthByAgentId[w.id]);
        container.appendChild(row);
      });
    });
  }

  function applyHealthToAllRows() {
    var rows = document.querySelectorAll('.sidebar-item[data-agent-id]');
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      var agentId = row.getAttribute('data-agent-id');
      applyHealthToRow(row, healthByAgentId[agentId]);
    }
  }

  function handleRowClick(target) {
    var row = target.closest('.sidebar-item');
    if (!row) return;
    var openNewBtn = target.closest('.sidebar-open-new');
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    if (openNewBtn) { openInNewWindow(agentId); return; }
    selectWorkspace(agentId);
  }
  document.addEventListener('click', function (e) { handleRowClick(e.target); });

  // Flip the hover affordance on the open-in-new button. Using delegated
  // listeners instead of CSS groups so we don't need Tailwind to generate
  // obscure group-hover:inline-flex-on-hidden rules.
  document.addEventListener('mouseover', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row || row.classList.contains('is-current')) return;
    var btn = row.querySelector('.sidebar-open-new');
    if (btn) { btn.classList.remove('hidden'); btn.classList.add('inline-flex'); }
  });
  document.addEventListener('mouseout', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row) return;
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    var btn = row.querySelector('.sidebar-open-new');
    if (btn) { btn.classList.add('hidden'); btn.classList.remove('inline-flex'); }
  });

  document.addEventListener('contextmenu', function (e) {
    var row = e.target.closest('.sidebar-item');
    if (!row) return;
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    if (agentId === currentWorkspaceId) { e.preventDefault(); return; }
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

  function handleChromeEvent(data) {
    if (data.type === 'workspaces') {
      lastWorkspaces = data.workspaces || [];
      renderWorkspaces(lastWorkspaces);
    } else if (data.type === 'workspace_server_status') {
      healthByAgentId = (data && data.health) ? data.health : Object.create(null);
      applyHealthToAllRows();
    }
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
