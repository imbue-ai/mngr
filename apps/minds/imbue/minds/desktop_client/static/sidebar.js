// Electron sidebar WebContentsView: renders the workspace list and wires
// clicks + context menus through window.minds IPC. In browser mode the
// chrome.js embedded sidebar handles the same job instead.
(function () {
  var isElectron = !!window.minds;
  var currentWorkspaceId = null;
  var lastWorkspaces = [];

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
        label.className = 'flex-1 whitespace-nowrap overflow-hidden text-ellipsis';
        label.textContent = w.name || w.id;
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
        container.appendChild(row);
      });
    });
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
      renderStuckToasts(data.stuck || []);
    }
  }

  // -- Stuck server toast --
  // The backend's _handle_chrome_events endpoint pushes
  // workspace_server_status events when an (agent_id, server_name) pair
  // crosses a consecutive-failure threshold or recovers. We render a toast
  // per stuck server with a Restart button that POSTs to the same endpoint
  // the landing-page restart button uses.

  function workspaceNameForAgentId(agentId) {
    for (var i = 0; i < lastWorkspaces.length; i += 1) {
      if (lastWorkspaces[i].id === agentId) return lastWorkspaces[i].name || agentId;
    }
    return agentId;
  }

  function formatStuckDuration(stuckSince) {
    var seconds = Math.max(0, Math.round(Date.now() / 1000 - stuckSince));
    if (seconds < 60) return seconds + 's';
    return Math.round(seconds / 60) + 'm';
  }

  function triggerRestart(button, agentId) {
    if (button.disabled) return;
    button.disabled = true;
    var original = button.textContent;
    button.textContent = 'Restarting...';
    fetch('/api/agents/' + encodeURIComponent(agentId) + '/restart-workspace-server', { method: 'POST' })
      .then(function (resp) {
        if (resp.ok) {
          button.textContent = 'Restart requested';
          return;
        }
        return resp.json().then(
          function (body) {
            button.textContent = 'Failed: ' + ((body && body.error) || ('HTTP ' + resp.status));
          },
          function () { button.textContent = 'Failed: HTTP ' + resp.status; },
        );
      })
      .catch(function (err) { button.textContent = 'Failed: ' + err; })
      .finally(function () {
        setTimeout(function () { button.disabled = false; button.textContent = original; }, 4000);
      });
  }

  function dismissToast(toastEl) {
    if (toastEl && toastEl.parentNode) toastEl.parentNode.removeChild(toastEl);
  }

  function renderStuckToasts(stuckServers) {
    var stack = document.getElementById('stuck-toast-stack');
    if (!stack) return;
    var keysInPayload = {};
    stuckServers.forEach(function (entry) {
      keysInPayload[entry.agent_id + '|' + entry.server_name] = entry;
    });
    Array.prototype.slice.call(stack.children).forEach(function (el) {
      var key = el.getAttribute('data-stuck-key');
      if (!keysInPayload[key]) dismissToast(el);
    });
    stuckServers.forEach(function (entry) {
      var key = entry.agent_id + '|' + entry.server_name;
      if (stack.querySelector('[data-stuck-key="' + key + '"]')) return;
      var toast = document.createElement('div');
      toast.setAttribute('data-stuck-key', key);
      toast.className = 'pointer-events-auto bg-zinc-800 text-zinc-200 border border-amber-500 rounded-md p-3 text-xs leading-snug shadow-lg';

      var title = document.createElement('div');
      title.className = 'font-semibold text-amber-400 mb-1';
      title.textContent = 'Workspace server not responding';
      toast.appendChild(title);

      var body = document.createElement('div');
      body.className = 'text-zinc-300 mb-2';
      body.textContent =
        'The "' + workspaceNameForAgentId(entry.agent_id) + '" mind has had '
        + entry.failure_count + ' failed requests over the last '
        + formatStuckDuration(entry.stuck_since) + '. '
        + 'Restarting the workspace server usually recovers it.';
      toast.appendChild(body);

      var disclosure = document.createElement('button');
      disclosure.className = 'bg-transparent border-0 text-zinc-400 p-0 cursor-pointer text-[11px] underline mb-1';
      disclosure.textContent = 'Show details';
      toast.appendChild(disclosure);

      var details = document.createElement('div');
      details.className = 'font-mono text-[11px] text-zinc-400 bg-black/30 rounded p-2 mb-2 whitespace-pre-wrap';
      details.hidden = true;
      details.textContent =
        'server: ' + entry.server_name + '\n'
        + 'agent_id: ' + entry.agent_id + '\n'
        + 'last_error: ' + entry.last_error_class + '\n'
        + 'failure_count: ' + entry.failure_count;
      toast.appendChild(details);

      disclosure.addEventListener('click', function () {
        details.hidden = !details.hidden;
        disclosure.textContent = details.hidden ? 'Show details' : 'Hide details';
      });

      var actions = document.createElement('div');
      actions.className = 'flex gap-1.5 justify-end';
      var dismissBtn = document.createElement('button');
      dismissBtn.className = 'bg-transparent border border-zinc-600 text-zinc-200 text-xs px-2.5 py-1 rounded cursor-pointer hover:bg-white/10';
      dismissBtn.textContent = 'Dismiss';
      dismissBtn.addEventListener('click', function () { dismissToast(toast); });
      actions.appendChild(dismissBtn);
      var restartBtn = document.createElement('button');
      restartBtn.className = 'bg-amber-700 border border-amber-700 text-white text-xs px-2.5 py-1 rounded cursor-pointer hover:bg-amber-600';
      restartBtn.textContent = 'Restart';
      restartBtn.addEventListener('click', function () { triggerRestart(restartBtn, entry.agent_id); });
      actions.appendChild(restartBtn);
      toast.appendChild(actions);

      stack.appendChild(toast);
    });
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
