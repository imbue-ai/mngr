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
//                                                    dataAttr, agentId, sizeClass);
//
// ``workspace`` is { id, name, accent?, is_stale? }. ``withOpenNew`` adds
// the "open in new window" arrow (Electron only -- browser mode has no
// multi-window concept and passes false). Both action icons are always
// visible. ``isCurrent`` marks the row selected (highlighted background).
// Event wiring (click / context-menu) is the caller's job -- this builds
// DOM only.
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

  // Settings gear (Icon16 ``settings``, Figma node 857-5131): the filled
  // outline, offset into the 16-unit frame to match the rest of the set.
  var SETTINGS_PATH =
    '<g transform="translate(0.5 0.5)"><path d="M8.33765 2.50001C8.33765 2.31436 8.26384 2.13617 8.13257 2.00489C8.0013 1.87363 7.82309 1.79981 7.63745 1.79981H7.36206C7.17669 1.79993 6.99907 1.87389 6.86792 2.00489C6.73664 2.13617 6.66284 2.31436 6.66284 2.50001V2.61329C6.66248 2.92882 6.57854 3.23854 6.42065 3.51173C6.263 3.78448 6.03608 4.01019 5.76343 4.16798L5.7644 4.16895L5.49487 4.3252L5.4939 4.32618C5.22026 4.48416 4.90947 4.56739 4.59351 4.56739C4.28352 4.56735 3.97951 4.48625 3.70972 4.33399V4.33497L3.61597 4.28517C3.61069 4.28235 3.60552 4.27936 3.60034 4.27638C3.43989 4.18382 3.24904 4.15837 3.07007 4.20606C2.89093 4.25397 2.73723 4.37177 2.64429 4.53224L2.50757 4.76954C2.41531 4.92995 2.39048 5.12004 2.43823 5.29884C2.47414 5.43311 2.54837 5.553 2.65112 5.64356L2.76343 5.72364L2.79272 5.7422L2.88647 5.8047H2.8855C3.14427 5.9608 3.36037 6.17885 3.51245 6.44044C3.67027 6.71193 3.75466 7.01998 3.75659 7.33399V7.65431L3.75269 7.77247C3.73559 8.04849 3.65566 8.31791 3.51733 8.5586C3.3607 8.83108 3.13439 9.05648 2.86304 9.21485L2.86401 9.21583L2.77026 9.27149L2.76343 9.27638C2.60297 9.36932 2.48613 9.52204 2.43823 9.70118C2.39048 9.87998 2.41531 10.0701 2.50757 10.2305L2.64429 10.4678C2.73723 10.6282 2.89093 10.7461 3.07007 10.794C3.24904 10.8417 3.43989 10.8162 3.60034 10.7236L3.61597 10.7149L3.70972 10.665C3.97941 10.5129 4.28368 10.4327 4.59351 10.4326C4.86998 10.4326 5.14235 10.4964 5.3894 10.6182L5.4939 10.6738L5.49487 10.6748L5.76245 10.8301L5.86304 10.8926C6.09166 11.0455 6.28249 11.2493 6.42065 11.4883C6.57854 11.7615 6.66248 12.0712 6.66284 12.3867V12.5C6.66284 12.6857 6.73664 12.8639 6.86792 12.9951C6.99907 13.1261 7.17669 13.2001 7.36206 13.2002H7.63745C7.82309 13.2002 8.0013 13.1264 8.13257 12.9951C8.26384 12.8639 8.33765 12.6857 8.33765 12.5V12.3867C8.33801 12.0713 8.42105 11.7614 8.57886 11.4883C8.71709 11.2492 8.90868 11.0455 9.13745 10.8926L9.23706 10.8301L9.50464 10.6748L9.50659 10.6738C9.78011 10.516 10.0902 10.4327 10.406 10.4326C10.7158 10.4326 11.0201 10.513 11.2898 10.665L11.3835 10.7149L11.4001 10.7236C11.5607 10.8162 11.7514 10.8418 11.9304 10.794C12.1096 10.7461 12.2623 10.6282 12.3552 10.4678L12.49 10.2295L12.4919 10.2256C12.5846 10.065 12.6102 9.87349 12.5623 9.69435C12.5148 9.51717 12.3998 9.36564 12.2419 9.27247L12.1599 9.2295C12.1545 9.2266 12.1487 9.22282 12.1433 9.21974C11.869 9.06123 11.6411 8.83328 11.4832 8.5586C11.3251 8.28367 11.2427 7.9714 11.2439 7.65431V7.34376C11.243 7.02734 11.3255 6.71579 11.4832 6.44142C11.6397 6.16926 11.8645 5.9425 12.1355 5.78419L12.2292 5.72853L12.2371 5.72364C12.3973 5.63068 12.5144 5.47785 12.5623 5.29884C12.61 5.11979 12.5845 4.92909 12.4919 4.76856V4.76759L12.3552 4.53224C12.2623 4.37177 12.1096 4.25397 11.9304 4.20606C11.7514 4.15823 11.5607 4.18386 11.4001 4.27638C11.3949 4.27942 11.3889 4.2823 11.3835 4.28517L11.3054 4.3252L11.3064 4.32618C11.0328 4.48416 10.722 4.56739 10.406 4.56739C10.0902 4.56735 9.78011 4.48405 9.50659 4.32618L9.50464 4.3252L9.23706 4.16895V4.16993C8.96387 4.01211 8.73675 3.78488 8.57886 3.51173C8.42105 3.23859 8.33801 2.92874 8.33765 2.61329V2.50001ZM8.82495 7.50001C8.82495 6.76823 8.23153 6.17481 7.49976 6.17481C6.76809 6.17495 6.17456 6.76831 6.17456 7.50001C6.17456 8.23171 6.76809 8.82507 7.49976 8.8252C8.23153 8.8252 8.82495 8.23179 8.82495 7.50001ZM9.92456 7.50001C9.92456 8.8393 8.83905 9.92481 7.49976 9.92481C6.16058 9.92468 5.07495 8.83922 5.07495 7.50001C5.07495 6.1608 6.16058 5.07534 7.49976 5.0752C8.83905 5.0752 9.92456 6.16072 9.92456 7.50001ZM9.43726 2.61231L9.44312 2.70313C9.45513 2.79393 9.48488 2.88212 9.53101 2.96192C9.57708 3.0416 9.63913 3.11035 9.71167 3.16603L9.78784 3.21778L9.78882 3.21876L10.0554 3.37403H10.0564C10.1627 3.43533 10.2833 3.46774 10.406 3.46778C10.5289 3.46778 10.6502 3.43547 10.7566 3.37403L10.7722 3.36427L10.866 3.31446C11.2757 3.08362 11.7598 3.02198 12.2146 3.14356C12.6753 3.26674 13.0684 3.56786 13.3074 3.98048L13.4451 4.21778V4.21876C13.6833 4.63172 13.7478 5.12244 13.6248 5.58302C13.5023 6.04104 13.2037 6.43153 12.7947 6.67091L12.7019 6.72755L12.6941 6.73243C12.5873 6.79411 12.4987 6.8833 12.4373 6.99024C12.3758 7.09719 12.343 7.21846 12.3435 7.34181V7.65821C12.343 7.78156 12.3758 7.90283 12.4373 8.00978C12.4987 8.11672 12.5873 8.20591 12.6941 8.26759L12.7712 8.3086L12.7878 8.31739C13.2004 8.55634 13.5015 8.94964 13.6248 9.41017C13.7475 9.86904 13.683 10.3575 13.447 10.7695L13.3103 11.0137L13.3074 11.0195C13.0684 11.4322 12.6753 11.7333 12.2146 11.8565C11.7597 11.9781 11.2758 11.9156 10.866 11.6846L10.7722 11.6358C10.7668 11.6329 10.7619 11.629 10.7566 11.626C10.6502 11.5645 10.5289 11.5322 10.406 11.5322C10.2833 11.5323 10.1627 11.5647 10.0564 11.626L10.0554 11.625L9.78882 11.7813L9.78784 11.7822C9.68156 11.8436 9.59244 11.9319 9.53101 12.0381C9.46964 12.1443 9.43745 12.2651 9.43726 12.3877V12.5C9.43726 12.9774 9.24748 13.4349 8.90991 13.7725C8.57235 14.11 8.11483 14.2998 7.63745 14.2998H7.36206C6.88483 14.2997 6.42706 14.1099 6.0896 13.7725C5.75217 13.4349 5.56226 12.9773 5.56226 12.5V12.3877C5.56207 12.2651 5.52988 12.1443 5.46851 12.0381C5.40708 11.932 5.31885 11.8436 5.21265 11.7822L5.21069 11.7813L4.94409 11.626L4.86108 11.5859C4.77655 11.551 4.68557 11.5322 4.59351 11.5322C4.47081 11.5323 4.35018 11.5647 4.2439 11.626C4.23855 11.6291 4.23274 11.6328 4.22729 11.6358L4.13354 11.6856L4.13257 11.6846C3.72316 11.915 3.24026 11.9778 2.78589 11.8565C2.32532 11.7333 1.93212 11.4321 1.69312 11.0195L1.55542 10.7822L1.55444 10.7813C1.31626 10.3683 1.25172 9.87753 1.37476 9.417C1.49724 8.95896 1.79581 8.56751 2.20483 8.32813L2.29858 8.27247L2.3064 8.26759C2.4132 8.20592 2.50178 8.11671 2.56323 8.00978C2.62462 7.90288 2.6565 7.78148 2.65601 7.65821V7.34083L2.65015 7.25001C2.63778 7.15974 2.60737 7.07245 2.56128 6.99317C2.49994 6.88771 2.41203 6.80032 2.3064 6.73927C2.29617 6.73336 2.28595 6.72629 2.27612 6.71974L2.18237 6.65724V6.65626C1.78541 6.4158 1.49488 6.03226 1.37476 5.58302C1.25172 5.12249 1.31626 4.63168 1.55444 4.21876L1.55542 4.21778L1.69312 3.98048C1.93212 3.56797 2.32532 3.26672 2.78589 3.14356C3.2401 3.02225 3.72325 3.08422 4.13257 3.31446H4.13354L4.22729 3.36427C4.23274 3.36717 4.23855 3.37095 4.2439 3.37403C4.35018 3.43533 4.47081 3.46774 4.59351 3.46778C4.71638 3.46778 4.83768 3.43547 4.94409 3.37403L5.21069 3.21876L5.21265 3.21778C5.31885 3.15646 5.40708 3.06806 5.46851 2.96192C5.51463 2.88214 5.54437 2.79391 5.5564 2.70313L5.56226 2.61231V2.50001C5.56226 2.02272 5.75217 1.56509 6.0896 1.22755C6.42706 0.890087 6.88483 0.700322 7.36206 0.700205H7.63745C8.11483 0.700205 8.57235 0.889999 8.90991 1.22755C9.24748 1.56511 9.43726 2.02262 9.43726 2.50001V2.61231Z"/></g>';

  function buildOpenNewBtn(agentId) {
    return buildIconButton('Open in new window', OPEN_NEW_PATH, 'data-open-new', agentId);
  }

  function buildSettingsBtn(agentId) {
    // Smaller glyph than the open-in-new arrow: a w-3.5 (14px) gear vs the
    // arrow's default w-4 (16px), both centered in the shared 24x24 button,
    // so the gear reads as a lighter secondary action.
    return buildIconButton('Workspace settings', SETTINGS_PATH, 'data-open-settings', agentId, 'w-3.5 h-3.5');
  }

  function buildRow(workspace, options) {
    var opts = options || {};
    var isCurrent = !!opts.isCurrent;
    var withOpenNew = !!opts.withOpenNew;

    // No outer margin: row-to-row spacing is the parent container's flex
    // ``gap``, keeping this element positioning-free and composable.
    var row = document.createElement('div');
    row.className =
      'sidebar-item group flex items-center gap-2 h-8 px-2 rounded-md cursor-pointer type-body text-primary'
      + (isCurrent ? ' is-current bg-fill-active' : ' hover:bg-fill-hover');
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
      staleDot.className = 'sidebar-stale-dot inline-block w-1.5 h-1.5 rounded-full bg-warning/80 shrink-0';
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
