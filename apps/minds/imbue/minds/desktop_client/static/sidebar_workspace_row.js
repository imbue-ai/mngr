// The workspace-menu list item, as a single shared builder. ``buildRow``
// is the one source of truth for a workspace row's markup; the icon-button
// helpers below are its internals (also exported for any standalone use).
// Loaded by every surface that shows the row so the markup lives in one
// place instead of being copy-pasted:
//   - sidebar.js   -- the Electron menu (modal WebContentsView)
//   - chrome.js    -- the browser-mode inline menu
//   - dev_styleguide.js -- the styleguide's "Sidebar items" sample
//
// Composability rule: the row carries NO outer positioning (no margin) --
// spacing between rows is owned by the parent container's flex ``gap``.
// Callers append the returned element into their own positioned container.
//
// Usage:
//   var row = window.mindsSidebarRow.buildRow(workspace,
//               { isCurrent: bool, withOpenNew: bool });
//   var btn = window.mindsSidebarRow.buildSettingsBtn(agentId);
//   var btn = window.mindsSidebarRow.buildOpenNewBtn(agentId);
//   var btn = window.mindsSidebarRow.buildIconButton(title, pathSvg,
//                                                    dataAttr, agentId);
//
// ``workspace`` is { id, name, accent?, is_stale? }. ``withOpenNew`` adds
// the "open in new window" arrow (Electron only -- browser mode has no
// multi-window concept and passes false). Both action icons are always
// visible. ``isCurrent`` marks the row selected (highlighted background).
// Event wiring (click / context-menu) is the caller's job -- this builds
// DOM only.
(function () {
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

  // lucide ``arrow-up-right`` (Figma "Space switcher menu", node 238-8163):
  // a bare diagonal arrow, not the old external-link box. Drawn in the
  // builder's 16px viewBox at lucide's standard 29.17% inset (span 4.67-11.33).
  var OPEN_NEW_PATH =
    '<path d="M11.33 11.33V4.67H4.67"/><path d="M11.33 4.67L4.67 11.33"/>';

  var SETTINGS_PATH =
    '<circle cx="8" cy="8" r="2"/>'
    + '<path d="M12.93 10a1.1 1.1 0 0 0 .22 1.21l.04.04a1.33 1.33 0 1 1-1.89 1.89l-.04-.04a1.1 1.1 0 0 0-1.21-.22 1.1 1.1 0 0 0-.67 1.01v.11a1.33 1.33 0 1 1-2.67 0v-.06A1.1 1.1 0 0 0 6 12.93a1.1 1.1 0 0 0-1.21.22l-.04.04a1.33 1.33 0 1 1-1.89-1.89l.04-.04A1.1 1.1 0 0 0 3.12 10a1.1 1.1 0 0 0-1.01-.67H2a1.33 1.33 0 1 1 0-2.67h.06A1.1 1.1 0 0 0 3.07 6a1.1 1.1 0 0 0-.22-1.21l-.04-.04a1.33 1.33 0 1 1 1.89-1.89l.04.04A1.1 1.1 0 0 0 6 3.12a1.1 1.1 0 0 0 .67-1.01V2a1.33 1.33 0 1 1 2.67 0v.06A1.1 1.1 0 0 0 10 3.07a1.1 1.1 0 0 0 1.21-.22l.04-.04a1.33 1.33 0 1 1 1.89 1.89l-.04.04A1.1 1.1 0 0 0 12.93 6a1.1 1.1 0 0 0 1.01.67H14a1.33 1.33 0 1 1 0 2.67h-.06a1.1 1.1 0 0 0-1.01.67z"/>';

  function buildOpenNewBtn(agentId) {
    return buildIconButton('Open in new window', OPEN_NEW_PATH, 'data-open-new', agentId);
  }

  function buildSettingsBtn(agentId) {
    return buildIconButton('Workspace settings', SETTINGS_PATH, 'data-open-settings', agentId);
  }

  function buildRow(workspace, options) {
    var opts = options || {};
    var isCurrent = !!opts.isCurrent;
    var withOpenNew = !!opts.withOpenNew;

    // No outer margin: row-to-row spacing is the parent container's flex
    // ``gap``, keeping this element positioning-free and composable.
    var row = document.createElement('div');
    row.className =
      'sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer text-[13px] text-white'
      + (isCurrent ? ' is-current bg-white/15' : ' hover:bg-white/5');
    row.setAttribute('data-agent-id', workspace.id);

    var dot = document.createElement('span');
    dot.className = 'sidebar-dot w-2.5 h-2.5 rounded-full shrink-0';
    row.appendChild(dot);

    var label = document.createElement('span');
    label.className = 'flex-1 whitespace-nowrap overflow-hidden text-ellipsis';
    label.textContent = workspace.name || workspace.id;
    row.appendChild(label);

    // Retained-but-unverified workspace (its provider's last discovery poll
    // errored): show an amber dot. The row stays fully clickable.
    if (workspace.is_stale) {
      row.classList.add('is-stale');
      var staleDot = document.createElement('span');
      staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-amber-400/80 shrink-0';
      staleDot.title = "This workspace's provider had a discovery error; its status is unverified (still usable).";
      row.appendChild(staleDot);
    }

    // Row action icons, always visible (no hover-reveal). The settings gear
    // is on every row in both modes; the open-in-new arrow is Electron-only
    // (withOpenNew) since the browser has no multi-window concept.
    function addActionIcon(btn) {
      btn.classList.add('inline-flex');
      row.appendChild(btn);
    }
    if (withOpenNew) addActionIcon(buildOpenNewBtn(workspace.id));
    addActionIcon(buildSettingsBtn(workspace.id));

    // Accent: prefer the server-attached value; otherwise resolve it
    // asynchronously via the shared workspace_accent helper (if loaded).
    var accent = typeof workspace.accent === 'string' ? workspace.accent : null;
    if (accent) {
      dot.style.background = accent;
      row.style.setProperty('--workspace-accent', accent);
    } else if (window.mindsAccent) {
      window.mindsAccent.get(workspace.id, function (c) {
        dot.style.background = c;
        row.style.setProperty('--workspace-accent', c);
      });
    }
    return row;
  }

  window.mindsSidebarRow = {
    buildIconButton: buildIconButton,
    buildOpenNewBtn: buildOpenNewBtn,
    buildSettingsBtn: buildSettingsBtn,
    buildRow: buildRow,
  };
})();
