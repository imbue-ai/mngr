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
//   var btn = window.mindsSidebarRow.buildOpenNewBtn(agentId);
//   var btn = window.mindsSidebarRow.buildIconButton(title, pathSvg,
//                                                    dataAttr, agentId, sizeClass);
//
// ``workspace`` is { id, name, accent?, is_stale?, backup_warning? }.
// ``withOpenNew`` adds the "open in new window" arrow to rows for OTHER
// workspaces (Electron only -- browser mode has no multi-window concept and
// omits it); the current row and remote rows carry no action buttons.
// ``isCurrent`` marks the row selected (highlighted background). Event
// wiring (click / context-menu) is the caller's job -- this builds DOM only.
(function () {
  function buildIconButton(title, pathSvg, dataAttr, agentId, sizeClass) {
    var btn = document.createElement('button');
    btn.type = 'button';
    // 24x24 hit area for easy clicking; the glyph keeps its own (smaller)
    // size via sizeClass and is centered inside the button.
    btn.className = 'sidebar-row-icon flex items-center justify-center w-6 h-6 bg-transparent border-none cursor-pointer text-secondary rounded-md hover:text-primary hover:bg-fill-hover';
    btn.title = title;
    btn.tabIndex = -1;
    btn.setAttribute(dataAttr, agentId);
    btn.innerHTML =
      '<svg class="' + (sizeClass || 'w-4 h-4') + '" viewBox="0 0 16 16" fill="currentColor">' +
      pathSvg + '</svg>';
    return btn;
  }

  // Open-in-new arrow (Icon16 ``arrow-up-right``, Figma node 857-5137): the
  // filled-outline diagonal arrow, shared with the Landing workspace rows.
  var OPEN_NEW_PATH =
    '<path d="M12.9331 10.3336C12.9329 10.6648 12.6646 10.9331 12.3335 10.9333C12.0022 10.9333 11.7331 10.6649 11.7329 10.3336V5.1149L4.09033 12.7575C3.85606 12.9916 3.47695 12.9916 3.24268 12.7575C3.00836 12.5232 3.00836 12.1432 3.24268 11.9088L10.8853 4.26627H5.6665C5.33513 4.26627 5.06689 3.99803 5.06689 3.66666C5.06689 3.33529 5.33513 3.06705 5.6665 3.06705H12.3335C12.6647 3.06722 12.9331 3.33539 12.9331 3.66666V10.3336Z"/>';

  function buildOpenNewBtn(agentId) {
    return buildIconButton('Open in new window', OPEN_NEW_PATH, 'data-open-new', agentId);
  }

  function buildRow(workspace, options) {
    var opts = options || {};
    var isCurrent = !!opts.isCurrent;
    var withOpenNew = !!opts.withOpenNew;

    // No outer margin: row-to-row spacing is the parent container's flex
    // ``gap``, keeping this element positioning-free and composable.
    var isRemote = !!workspace.is_remote;
    var row = document.createElement('div');
    row.className =
      'sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md type-body '
      + (isRemote
        ? 'is-remote text-secondary opacity-60 cursor-default'
        : ('cursor-pointer text-primary' + (isCurrent ? ' is-current bg-fill-active' : ' hover:bg-fill-hover')));
    row.setAttribute('data-agent-id', workspace.id);

    var dot = document.createElement('span');
    dot.className = 'sidebar-dot w-2.5 h-2.5 rounded-full shrink-0';
    row.appendChild(dot);

    var label = document.createElement('span');
    label.className = 'flex-1 whitespace-nowrap overflow-hidden text-ellipsis';
    label.textContent = workspace.name || workspace.id;
    row.appendChild(label);

    // A workspace hosted on another device (known via its synced record):
    // greyed, non-navigable, with a location badge instead of action icons.
    if (isRemote) {
      var locationBadge = document.createElement('span');
      locationBadge.className = 'inline-flex items-center px-1.5 py-0.5 rounded-md type-label bg-fill-subtle text-tertiary shrink-0';
      locationBadge.textContent = 'on ' + (workspace.location || 'another device');
      row.appendChild(locationBadge);
    }

    // Backup-service problem detected for this workspace (outdated code,
    // drifted credentials, service down, unconfigured, or unverifiable):
    // one warning badge style for all causes; the tooltip carries the
    // distinction. Fed by the shared /_static/backup_health.js cache.
    var backupWarning = workspace.backup_warning ||
      (window.mindsBackupHealth ? window.mindsBackupHealth.get(workspace.id) : null);
    if (backupWarning) {
      row.classList.add('has-backup-warning');
      var backupDot = document.createElement('span');
      backupDot.className = 'sidebar-backup-dot inline-block w-1.5 h-1.5 rounded-full bg-warning shrink-0';
      backupDot.title = backupWarning;
      row.appendChild(backupDot);
    }

    // Retained-but-unverified workspace (its provider's last discovery poll
    // errored): show an amber dot. The row stays fully clickable.
    if (workspace.is_stale) {
      row.classList.add('is-stale');
      var staleDot = document.createElement('span');
      staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-warning/80 shrink-0';
      staleDot.title = "This workspace's provider had a discovery error; its status is unverified (still usable).";
      row.appendChild(staleDot);
    }

    // Row action icon, always visible (no hover-reveal): the open-in-new
    // arrow, only on rows for OTHER local workspaces (withOpenNew; Electron
    // only). The current row and remote rows carry no action buttons.
    if (withOpenNew && !isCurrent && !isRemote) {
      var openNewBtn = buildOpenNewBtn(workspace.id);
      openNewBtn.classList.add('inline-flex');
      row.appendChild(openNewBtn);
    }

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
    buildRow: buildRow,
  };
})();
