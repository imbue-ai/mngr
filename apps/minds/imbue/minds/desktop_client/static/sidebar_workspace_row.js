// Shared icon-button builders for the sidebar workspace rows. Loaded by
// both Chrome.jinja (browser-mode inline sidebar in chrome.js) and
// Sidebar.jinja (the sidebar page loaded into the shared modal
// WebContentsView in sidebar.js) so the 16px stroke icon markup and the
// workspace-settings gear SVG path live in one place rather than being
// copy-pasted into every per-page script.
//
// Usage:
//   var btn = window.mindsSidebarRow.buildSettingsBtn(agentId);
//   var btn = window.mindsSidebarRow.buildOpenNewBtn(agentId);
//   var btn = window.mindsSidebarRow.buildIconButton(title, pathSvg,
//                                                    dataAttr, agentId);
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

  var OPEN_NEW_PATH =
    '<path d="M9.33 2.67h4v4"/><path d="M6.67 9.33L13.33 2.67"/>'
    + '<path d="M13.33 9.33v3.33a1.33 1.33 0 0 1-1.33 1.33H3.33a1.33 1.33 0 0 1-1.33-1.33V4a1.33 1.33 0 0 1 1.33-1.33h3.33"/>';

  var SETTINGS_PATH =
    '<circle cx="8" cy="8" r="2"/>'
    + '<path d="M12.93 10a1.1 1.1 0 0 0 .22 1.21l.04.04a1.33 1.33 0 1 1-1.89 1.89l-.04-.04a1.1 1.1 0 0 0-1.21-.22 1.1 1.1 0 0 0-.67 1.01v.11a1.33 1.33 0 1 1-2.67 0v-.06A1.1 1.1 0 0 0 6 12.93a1.1 1.1 0 0 0-1.21.22l-.04.04a1.33 1.33 0 1 1-1.89-1.89l.04-.04A1.1 1.1 0 0 0 3.12 10a1.1 1.1 0 0 0-1.01-.67H2a1.33 1.33 0 1 1 0-2.67h.06A1.1 1.1 0 0 0 3.07 6a1.1 1.1 0 0 0-.22-1.21l-.04-.04a1.33 1.33 0 1 1 1.89-1.89l.04.04A1.1 1.1 0 0 0 6 3.12a1.1 1.1 0 0 0 .67-1.01V2a1.33 1.33 0 1 1 2.67 0v.06A1.1 1.1 0 0 0 10 3.07a1.1 1.1 0 0 0 1.21-.22l.04-.04a1.33 1.33 0 1 1 1.89 1.89l-.04.04A1.1 1.1 0 0 0 12.93 6a1.1 1.1 0 0 0 1.01.67H14a1.33 1.33 0 1 1 0 2.67h-.06a1.1 1.1 0 0 0-1.01.67z"/>';

  function buildOpenNewBtn(agentId) {
    return buildIconButton('Open in new window', OPEN_NEW_PATH, 'data-open-new', agentId);
  }

  function buildSettingsBtn(agentId) {
    return buildIconButton('Workspace settings', SETTINGS_PATH, 'data-open-settings', agentId);
  }

  window.mindsSidebarRow = {
    buildIconButton: buildIconButton,
    buildOpenNewBtn: buildOpenNewBtn,
    buildSettingsBtn: buildSettingsBtn,
  };
})();
