// Workspace-menu overlay module. Registers the sidebar in the overlay host's
// registry so overlay.js renders it as in-page DOM: it fetches
// /_chrome/sidebar?...&fragment=1 (the server positions the floating menu via
// inline style from the trigger geometry in the URL) and injects the panel,
// then calls this module's init(container). It is scoped to the injected
// container and driven by the host's cached SSE state
// (window.MINDS_OVERLAY_HOST) so the workspace list is current the instant the
// menu opens. The host owns the backdrop click-outside dismiss and main owns
// Escape, so this module wires neither. (The browser shows the workspace menu
// via chrome.js's inline menu instead, not this module.)
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  var cleanups = [];

  window.MINDS_OVERLAY_MODALS.sidebar = {
    // Full-window transparent backdrop with a server-positioned floating menu;
    // the host wires click-outside-menu dismiss (a click on the backdrop).
    positioning: 'backdrop',

    init: function (container) {
      var host = window.MINDS_OVERLAY_HOST || {};
      var backdrop = container.querySelector('#sidebar-backdrop');
      // The mngr-forward plugin's bare origin, exposed as a data attribute on
      // the fragment (the full page put it on <body>, which fragments lack).
      var mngrForwardOrigin = (backdrop && backdrop.dataset.mngrForwardOrigin) || '';
      var currentWorkspaceId = (host.getCurrentWorkspaceId && host.getCurrentWorkspaceId()) || null;
      var lastWorkspaces = [];
      var signedIn = false;

      function find(selector) {
        return container.querySelector(selector);
      }

      function navigate(url) {
        if (window.minds && window.minds.navigateContent) window.minds.navigateContent(url);
      }
      function selectWorkspace(agentId) {
        navigate(mngrForwardOrigin + '/goto/' + agentId + '/');
      }
      function openInNewWindow(agentId) {
        if (window.minds && window.minds.openWorkspaceInNewWindow) window.minds.openWorkspaceInNewWindow(agentId);
      }
      function openWorkspaceSettings(agentId) {
        navigate('/workspace/' + agentId + '/settings');
      }

      function renderWorkspaces(workspaces) {
        var listContainer = find('#sidebar-workspaces');
        if (!listContainer) return;
        listContainer.textContent = '';
        if (!workspaces || workspaces.length === 0) return;
        var groups = {};
        workspaces.forEach(function (workspace) {
          var key = workspace.account || 'Private';
          if (!groups[key]) groups[key] = [];
          groups[key].push(workspace);
        });
        var keys = Object.keys(groups).sort(function (leftKey, rightKey) {
          if (leftKey === 'Private') return -1;
          if (rightKey === 'Private') return 1;
          return leftKey.localeCompare(rightKey);
        });
        keys.forEach(function (key, keyIndex) {
          if (keyIndex > 0 || keys.length > 1) {
            var header = document.createElement('div');
            header.className = 'px-2 pt-2 pb-1 type-section text-tertiary';
            header.textContent = key === 'Private' ? 'Private' : key;
            listContainer.appendChild(header);
          }
          groups[key].forEach(function (workspace) {
            // Shared row builder; withOpenNew:true since Electron supports
            // multi-window. Spacing is owned by the container's flex gap.
            listContainer.appendChild(
              window.mindsSidebarRow.buildRow(workspace, {
                isCurrent: workspace.id === currentWorkspaceId,
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
        if (target.closest('[data-open-settings]')) { openWorkspaceSettings(agentId); return; }
        selectWorkspace(agentId);
      }

      function onClick(event) {
        if (event.target.closest('#sidebar-new-workspace')) { navigate('/create'); return; }
        if (event.target.closest('#sidebar-settings')) { navigate('/settings'); return; }
        if (event.target.closest('#sidebar-account')) { navigate(signedIn ? '/accounts' : '/auth/login'); return; }
        handleRowClick(event.target);
      }
      container.addEventListener('click', onClick);

      function onContextMenu(event) {
        var row = event.target.closest('.sidebar-item');
        if (!row) return;
        var agentId = row.getAttribute('data-agent-id');
        if (!agentId) return;
        event.preventDefault();
        if (window.minds && window.minds.showWorkspaceContextMenu) {
          window.minds.showWorkspaceContextMenu(agentId, event.clientX, event.clientY);
        }
      }
      container.addEventListener('contextmenu', onContextMenu);

      // Auth status: the SSE stream carries workspaces but not auth transitions,
      // so poll /auth/api/status now and whenever the content view navigates
      // (a sign-in / sign-out happens there). Mirrors chrome.js's browser menu.
      function updateAccountUI(data) {
        var label = find('#sidebar-account-label');
        var accountButton = find('#sidebar-account');
        if (!label || !accountButton) return;
        if (data && data.signedIn) {
          signedIn = true;
          label.textContent = 'Manage account(s)';
          accountButton.title = data.email || 'Manage accounts';
        } else {
          signedIn = false;
          label.textContent = 'Log in';
          accountButton.title = 'Sign in to your account';
        }
      }
      function refreshAuthStatus() {
        fetch('/auth/api/status')
          .then(function (response) { return response.json(); })
          .then(updateAccountUI)
          .catch(function () {});
      }
      refreshAuthStatus();

      // Stay live while open: re-render on workspace-list changes and current-
      // workspace changes; re-poll auth on content navigations. Each host
      // subscription returns an unsubscribe, dropped on destroy.
      if (host.onCurrentWorkspaceChanged) {
        cleanups.push(host.onCurrentWorkspaceChanged(function (agentId) {
          currentWorkspaceId = agentId || null;
          renderWorkspaces(lastWorkspaces);
        }));
      }
      if (host.onChromeEvent) {
        cleanups.push(host.onChromeEvent(function (data) {
          if (!data || data.type !== 'workspaces') return;
          lastWorkspaces = data.workspaces || [];
          renderWorkspaces(lastWorkspaces);
        }));
      }
      if (host.onContentURLChange) {
        cleanups.push(host.onContentURLChange(refreshAuthStatus));
      }

      // Initial paint from the host's cached workspace list (current the instant
      // we open -- no priming round-trip).
      var cachedWorkspaces = host.getChromeEvent && host.getChromeEvent('workspaces');
      lastWorkspaces = (cachedWorkspaces && cachedWorkspaces.workspaces) || [];
      renderWorkspaces(lastWorkspaces);
    },

    destroy: function () {
      cleanups.forEach(function (cleanup) { try { cleanup(); } catch (error) { /* noop */ } });
      cleanups = [];
    },
  };
})();
