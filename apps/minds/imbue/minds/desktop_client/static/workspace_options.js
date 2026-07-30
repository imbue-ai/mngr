// The docked workspace-options panel: tab + group navigation, and the whole
// "Share machine" pane.
//
// Three pages load it. The overlay-hosted panel (pages.WorkspaceOptionsModal,
// Electron) and its full-page twin (pages.WorkspaceOptions, browser mode)
// render the same markup and use all of it. The standalone settings page
// (pages.WorkspaceSettings) renders only WorkspaceSettingsSections, so it uses
// just the Machine settings group nav and returns at the missing
// #ws-share-config. Everything is guarded on its own elements, which is what
// lets a surface omit a pane.
//
// Sharing is per (workspace, service). "Whole machine" is not a special case:
// it is the workspace's own web UI service (system_interface), shared through
// the same tunnel + Cloudflare Access policy as any app. Each target therefore
// keeps its own independent state, cached here per target so switching back
// and forth does not re-shell-out to the imbue_cloud connector.
//
// The ACL is rebuilt with DOM methods (never innerHTML) so a crafted email
// cannot inject script, matching sharing.js.

(function () {
  'use strict';

  // The full-page auth flow, which the shell replaces with its sign-in modal.
  // A completed sign-in lands the content view on the workspace list; the
  // panel's own URL is not a content path, so it cannot be the return target.
  var AUTH_LOGIN_PATH = '/auth/login';
  var SIGNIN_RETURN_PATH = '/';

  // -- Panel chrome ---------------------------------------------------------

  // Dismissal: in Electron the panel is an overlay iframe, so it must ask the
  // shell to tear the overlay down; as a plain page there is nothing to close,
  // so fall back to the workspace. ``/goto/<agent>/`` is served by the mngr
  // forward plugin, NOT by minds' own origin, so it needs the plugin origin the
  // page carries on its body (same read as sharing.js / sidebar.js); without
  // one there is no workspace URL to build, so land on the workspace list.
  window.dismissWorkspaceOptions = function () {
    var backdrop = document.getElementById('ws-options-backdrop');
    var agentId = backdrop ? backdrop.dataset.agentId : '';
    if (window.minds && window.minds.closeModal) {
      window.minds.closeModal();
      return;
    }
    var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';
    window.location.href = agentId && mngrForwardOrigin
      ? mngrForwardOrigin + '/goto/' + encodeURIComponent(agentId) + '/'
      : '/';
  };

  var backdropEl = document.getElementById('ws-options-backdrop');
  if (backdropEl) {
    backdropEl.addEventListener('click', function (event) {
      if (event.target === backdropEl) window.dismissWorkspaceOptions();
    });

    // No link inside the panel may navigate the overlay iframe: the shell's
    // overlay guard only blocks FOREIGN origins, so a same-origin href (the
    // backups page's "View all backups", the Associate prompt's sign-in link)
    // would load a full page into the modal view and strand the app there.
    // Hand those to the shell instead and dismiss, the way SharingModal does
    // for its own sign-in link. Only present on the overlay surface -- the
    // browser-mode page and the standalone settings page navigate normally.
    document.addEventListener('click', function (event) {
      if (!window.minds || !window.minds.navigateContent) return;
      // Let the browser handle new-tab / new-window intents unchanged.
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      var link = event.target.closest ? event.target.closest('a[href]') : null;
      if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
      var target;
      try {
        target = new URL(link.getAttribute('href'), window.location.href);
      } catch (_) {
        return;
      }
      // Only a real same-origin page load has to be redirected. An anchor that
      // resolves back to this page is going nowhere -- backup_table.js's
      // Download rows are href="#" plus their own preventDefault, and handing
      // the panel's own URL to the content view would be a spurious navigation.
      if (target.origin !== window.location.origin) return;
      if (target.pathname === window.location.pathname && target.search === window.location.search) return;
      event.preventDefault();
      // Signing in is a modal in the desktop shell, so the Associate prompt's
      // "Sign in or create an account" opens that rather than sending the whole
      // app off to the full-page auth flow -- the panel is where the user is
      // working, and the full page is only the browser-mode fallback.
      if (target.pathname === AUTH_LOGIN_PATH && window.minds.openSigninModal) {
        window.minds.openSigninModal(SIGNIN_RETURN_PATH);
        return;
      }
      window.minds.navigateContent(target.pathname + target.search + target.hash);
      window.dismissWorkspaceOptions();
    });
  }


  // Tab switching happens in place -- both panes are server-rendered, so a
  // switch never reloads the overlay iframe (which would flash) and never
  // loses the pane's state.
  function selectTab(tabId) {
    var tabs = document.querySelectorAll('[data-wsopt-tab]');
    if (!tabs.length) return;
    Array.prototype.forEach.call(tabs, function (tab) {
      var isSelected = tab.dataset.wsoptTab === tabId;
      tab.setAttribute('aria-selected', isSelected ? 'true' : 'false');
      // The selected tab is filled with the card's own surface and
      // square-bottomed so it reads as joined to the panel below it.
      tab.classList.toggle('bg-surface-primary', isSelected);
      tab.classList.toggle('rounded-b-none', isSelected);
      tab.classList.toggle('text-primary', isSelected);
      // Only an unselected tab sits on the accent-tinted titlebar and needs
      // its self-theming; the selected one is on the card's own surface.
      tab.classList.toggle('titlebar-surface', !isSelected);
      tab.classList.toggle('cursor-pointer', !isSelected);
      tab.classList.toggle('text-secondary', !isSelected);
      tab.classList.toggle('hover:bg-fill-hover', !isSelected);
      tab.classList.toggle('active:bg-fill-active', !isSelected);
      tab.classList.toggle('hover:text-primary', !isSelected);
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-wsopt-panel]'), function (panel) {
      var isShown = panel.dataset.wsoptPanel === tabId;
      panel.classList.toggle('hidden', !isShown);
      // A shown panel is the flex column that gives its title a pinned row and
      // its right pane the leftover height to scroll in; ``hidden`` and
      // ``flex`` would fight, so only lay it out while shown.
      panel.classList.toggle('flex', isShown);
    });
    rememberInUrl('tab', tabId);
  }

  // Keep ?tab= and ?group= pointing at what is actually on screen. Several
  // controls in these panes finish with window.location.reload() (rename, link,
  // unlink); without this the reload replays the URL the panel was OPENED with,
  // so acting on Machine settings would drop the user back on Share, and
  // linking an account would drop them from Account back to General -- away
  // from the control they had just used. The anchor params must survive
  // untouched (they position the panel's tab strip), so only the named param is
  // rewritten, and in place so no history entry is added.
  function rememberInUrl(param, value) {
    if (!window.history || !window.history.replaceState) return;
    var here;
    try {
      here = new URL(window.location.href);
    } catch (_) {
      return;
    }
    if (here.searchParams.get(param) === value) return;
    here.searchParams.set(param, value);
    window.history.replaceState(window.history.state, '', here.pathname + here.search + here.hash);
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-wsopt-tab]'), function (tab) {
    tab.addEventListener('click', function () { selectTab(tab.dataset.wsoptTab); });
  });

  // Reopening this panel while it is already on screen (the other titlebar
  // tab) hands it the new URL rather than remounting the whole page -- both
  // panes are already here, so the switch is the same in-place one a tab click
  // does. Answering false sends the host back to a fresh mount.
  //
  // The anchor pixels are baked into the server-rendered tab strip, so a
  // different anchor genuinely needs a re-render and is declined here.
  window.mindsOverlayUpdate = function (rawUrl) {
    var next;
    try {
      next = new URL(rawUrl, window.location.href);
    } catch (_) {
      return false;
    }
    if (next.pathname !== window.location.pathname) return false;
    var here = new URLSearchParams(window.location.search);
    var there = next.searchParams;
    var anchorParams = ['x', 'y', 'h'];
    for (var i = 0; i < anchorParams.length; i++) {
      if ((here.get(anchorParams[i]) || '') !== (there.get(anchorParams[i]) || '')) return false;
    }
    var tab = there.get('tab');
    if (tab) selectTab(tab);
    var group = there.get('group');
    if (group) selectGroup(group);
    // The settings-only page has no share pane, so it has no target to select.
    var target = there.get('target');
    if (target && document.getElementById('ws-share-config')) selectTarget(target);
    return true;
  };

  // -- Machine settings group nav -------------------------------------------

  function selectGroup(groupId) {
    var target = document.querySelector('[data-settings-group="' + groupId + '"]');
    if (target) target.click();
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-settings-group]'), function (button) {
    button.addEventListener('click', function () {
      var groupId = button.dataset.settingsGroup;
      Array.prototype.forEach.call(document.querySelectorAll('[data-settings-group]'), function (other) {
        var isSelected = other === button;
        other.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
        other.classList.toggle('bg-fill-hover', isSelected);
        other.classList.toggle('font-semibold', isSelected);
      });
      Array.prototype.forEach.call(document.querySelectorAll('[data-settings-pane]'), function (pane) {
        pane.classList.toggle('hidden', pane.dataset.settingsPane !== groupId);
      });
      rememberInUrl('group', groupId);
    });
  });

  // -- Share machine pane ---------------------------------------------------

  var configEl = document.getElementById('ws-share-config');
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);
  var agentId = config.agentId;
  var wholeService = config.wholeService;
  var ownerEmail = config.accountEmail || '';

  // Per-target state, filled lazily on first selection:
  //   { loaded, failed, enabled, url, emails, isLive }
  // ``emails`` excludes the owner, which is always implicitly first and is
  // never removable (the account owns the tunnel). ``failed`` is deliberately
  // NOT ``loaded``: a status read that never landed must not masquerade as a
  // read that came back "sharing is off", or enabling from that pane would
  // replace an access policy nobody ever saw.
  var stateByTarget = {};
  var currentTarget = config.selectedTarget || wholeService;
  var readinessTimer = null;
  // Which target ``readinessTimer`` belongs to, so re-rendering the same
  // target does not restart its clock (null whenever no poll is armed).
  var pollingService = null;
  // The full-page twin is in the shell's in-place swap set, so its document
  // outlives the page: without this, each visit would leave another live
  // delegated handler (whose stale closure rewrites the ACL and re-PUTs the
  // Access policy) plus a readiness poll still hitting the server.
  var isPageTornDown = false;

  function el(id) { return document.getElementById(id); }

  function targetLabel(service) {
    return service === wholeService ? 'Whole machine' : service;
  }

  function targetSubtitle(service) {
    return service === wholeService
      ? 'Give access to everything in this machine.'
      : 'Give access only to this app on its own.';
  }

  function stateFor(service) {
    if (!stateByTarget[service]) {
      stateByTarget[service] = { loaded: false, failed: false, enabled: false, url: '', emails: [], isLive: false };
    }
    return stateByTarget[service];
  }

  // A completed write tells us this target's state as authoritatively as a read
  // does, so it clears any earlier read failure. Without this a write that
  // landed while the state was still unknown would leave ``loaded`` false and
  // renderTarget would show neither the Enable control nor the link -- a blank
  // pane reporting nothing at all.
  function markKnown(state) {
    state.loaded = true;
    state.failed = false;
  }

  function showError(message) {
    var errorEl = el('ws-share-error');
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function clearError() {
    var errorEl = el('ws-share-error');
    if (errorEl) errorEl.classList.add('hidden');
  }

  // ``fetch`` only rejects on network failure -- a 4xx/5xx response is a
  // successful Promise. Wrap it so callers treat transport errors and
  // server-side errors uniformly (same contract as sharing.js).
  function requestWithErrorCheck(url, options) {
    return fetch(url, options).then(function (response) {
      if (response.ok) return response;
      return response.text().then(function (text) {
        var detail = text;
        try {
          detail = window.normalizeApiError(JSON.parse(text)).message;
        } catch (_) { /* leave detail as the raw body */ }
        var error = new Error(detail || ('HTTP ' + response.status));
        error.httpStatus = response.status;
        throw error;
      });
    });
  }

  function sharingUrlFor(service) {
    return '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/sharing/' + encodeURIComponent(service);
  }

  function createAclRow(email, isOwner) {
    var row = document.createElement('div');
    row.className = 'flex items-center justify-between gap-2 rounded-md border border-subtle bg-fill-subtle px-3 py-2';

    var label = document.createElement('span');
    label.className = 'type-body text-primary truncate';
    label.textContent = email;
    if (isOwner) {
      var suffix = document.createElement('span');
      suffix.className = 'text-tertiary';
      suffix.textContent = ' (you)';
      label.appendChild(suffix);
    }
    row.appendChild(label);

    if (!isOwner) {
      var removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'shrink-0 inline-flex h-6 w-6 items-center justify-center rounded-md ' +
        'text-tertiary hover:bg-fill-hover hover:text-important cursor-pointer transition-colors';
      // aria-label carries the whole affordance, as it does for sharing.js's
      // remove button: tooltip_triggers.js binds ``data-tooltip`` once at load,
      // so a row built here -- long after that pass -- could never get one.
      removeBtn.setAttribute('aria-label', 'Remove ' + email);
      removeBtn.dataset.removeEmail = email;
      // The icon markup mirrors Icon16's ``close`` glyph; the path data lives
      // in templates.py and cannot be reached from JS, so the shape is
      // inlined here (the one place JS renders an icon).
      removeBtn.appendChild(makeCloseIcon());
      row.appendChild(removeBtn);
    }
    return row;
  }

  function makeCloseIcon() {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'w-3.5 h-3.5');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('aria-hidden', 'true');
    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M11.5762 3.57617C11.8105 3.34186 12.1895 3.34186 12.4238 3.57617C12.6581 3.81049 ' +
      '12.6581 4.18951 12.4238 4.42383L8.84766 8L12.4238 11.5762C12.6581 11.8105 12.6581 12.1895 12.4238 ' +
      '12.4238C12.1895 12.6581 11.8105 12.6581 11.5762 12.4238L8 8.84766L4.42383 12.4238C4.18951 12.6581 ' +
      '3.81049 12.6581 3.57617 12.4238C3.34186 12.1895 3.34186 11.8105 3.57617 11.5762L7.15234 8L3.57617 ' +
      '4.42383C3.34186 4.18951 3.34186 3.81049 3.57617 3.57617C3.81049 3.34186 4.18951 3.34186 4.42383 ' +
      '3.57617L8 7.15234L11.5762 3.57617Z');
    svg.appendChild(path);
    return svg;
  }

  function renderAcl() {
    var listEl = el('ws-share-emails');
    if (!listEl) return;
    var state = stateFor(currentTarget);
    listEl.textContent = '';
    if (ownerEmail) listEl.appendChild(createAclRow(ownerEmail, true));
    state.emails.forEach(function (email) {
      listEl.appendChild(createAclRow(email, false));
    });
  }

  function finalEmails() {
    var state = stateFor(currentTarget);
    var emails = ownerEmail ? [ownerEmail] : [];
    return emails.concat(state.emails);
  }

  // The status line under the editor, for a wait whose own control is not on
  // screen to carry it.
  function setBusyLine(isBusy, label) {
    var busyEl = el('ws-share-busy');
    if (busyEl) {
      busyEl.classList.toggle('hidden', !isBusy);
      // ``hidden`` and ``flex`` would fight; only lay it out while shown.
      busyEl.classList.toggle('flex', isBusy);
    }
    var busyLabel = el('ws-share-busy-label');
    if (busyLabel && label) busyLabel.textContent = label;
  }

  // Which slow write each target has in flight, keyed by service rather than
  // held in one flag for the pane. A single flag went stale the moment the user
  // started a write and switched targets: the new target inherited the old
  // one's spinner and locked editor, and the wait it described was not even
  // happening there. Every target now shows its own truth, and coming back to
  // one with a write still running finds it still busy.
  var pendingByTarget = {};

  function startPending(service, kind) {
    pendingByTarget[service] = kind;
    // Through renderTarget, not applyPending: a pending write also decides
    // which rows are on screen (a disable takes the link away immediately).
    if (service === currentTarget) renderTarget();
  }

  function endPending(service) {
    delete pendingByTarget[service];
    if (service === currentTarget) renderTarget();
  }

  // Paint the current target's in-flight write (if any).
  //
  // Enable has its line to itself, so its button becomes a spinner and the
  // sentence explaining the wait sits beside it. Disable takes the link row
  // away the moment it is pressed (renderTarget), taking its own button with
  // it, so its spinner and sentence go to the status line below. Either way the
  // rest of the editor locks, so nothing can be staged against a policy that is
  // mid-write.
  function applyPending() {
    var kind = pendingByTarget[currentTarget] || '';
    var busy = window.mindsButtonBusy;
    var enableBtn = el('ws-share-enable-btn');
    if (enableBtn) {
      if (kind === 'enable') busy.set(enableBtn, '', 'inverse');
      else busy.clear(enableBtn);
    }
    var enableStatus = el('ws-share-enable-status');
    if (enableStatus) enableStatus.classList.toggle('hidden', kind !== 'enable');
    if (kind === 'disable') setBusyLine(true, 'Stopping sharing and revoking the link...');
    // An email edit changes the list rather than any one button, so its wait
    // goes to the status line too.
    else setBusyLine(kind === 'emails', 'Updating who can open this link...');
    // A target whose status never loaded cannot be edited either (see below).
    setEditable(stateFor(currentTarget).loaded && !kind);
  }

  // A target whose status never loaded has no Enable control on screen, so an
  // address added there could not be published -- and the next read would
  // overwrite it from the server without a word. Lock the editor instead of
  // letting the user stage something that quietly goes nowhere.
  function setEditable(isEditable) {
    ['ws-share-add-btn', 'ws-share-new-email'].forEach(function (id) {
      var node = el(id);
      if (node) node.disabled = !isEditable;
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-remove-email]'), function (node) {
      node.disabled = !isEditable;
    });
  }

  function renderTarget() {
    var state = stateFor(currentTarget);
    var isWhole = currentTarget === wholeService;

    var nameEl = el('ws-share-target-name');
    if (nameEl) nameEl.textContent = targetLabel(currentTarget);
    var subtitleEl = el('ws-share-target-subtitle');
    if (subtitleEl) subtitleEl.textContent = targetSubtitle(currentTarget);
    var appIcon = el('ws-share-icon-app');
    if (appIcon) appIcon.classList.toggle('hidden', isWhole);
    var wholeIcon = el('ws-share-icon-whole');
    if (wholeIcon) wholeIcon.classList.toggle('hidden', !isWhole);

    Array.prototype.forEach.call(document.querySelectorAll('[data-share-target]'), function (button) {
      var isSelected = button.dataset.shareTarget === currentTarget;
      button.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
      button.classList.toggle('bg-fill-hover', isSelected);
      button.classList.toggle('font-semibold', isSelected);
    });

    var loadingEl = el('ws-share-loading');
    // A failed read is done loading but is NOT loaded: the error line stands in
    // for the pane, and no control that would write a policy is offered.
    if (loadingEl) loadingEl.classList.toggle('hidden', state.loaded || state.failed);
    // A disable takes the link away the moment it is asked for, rather than
    // leaving a link on screen that is already being revoked. Neither row shows
    // while it runs -- the status line below carries the wait, and the Enable
    // button reappears when the server confirms the link is gone.
    var isDisabling = pendingByTarget[currentTarget] === 'disable';
    var enableRow = el('ws-share-enable-row');
    if (enableRow) {
      var showEnable = state.loaded && !state.enabled && !isDisabling;
      enableRow.classList.toggle('hidden', !showEnable);
      enableRow.classList.toggle('flex', showEnable);
    }
    var urlRow = el('ws-share-url-row');
    if (urlRow) {
      var showUrl = state.loaded && state.enabled && !isDisabling;
      urlRow.classList.toggle('hidden', !showUrl);
      // The row's layout classes only apply once it is not hidden; ``hidden``
      // and ``flex`` would otherwise fight (see Badge.jinja's note).
      urlRow.classList.toggle('flex', showUrl);
    }
    var urlEl = el('ws-share-url');
    if (urlEl) urlEl.textContent = state.url || '';
    var provisioningEl = el('ws-share-provisioning');
    // Also gone while a disable runs: the link this notice is about has already
    // come off screen, so explaining that Cloudflare is still publishing it
    // describes something the user can no longer see.
    if (provisioningEl) provisioningEl.classList.toggle('hidden', !isAwaitingLink(state) || isDisabling);
    renderAcl();
    applyPending();
    // A visible notice always has a poll behind it, or it would never clear.
    // The reverse is allowed on purpose: a disable in flight hides the notice
    // but leaves the poll running, because a DELETE that fails leaves the
    // target still enabled and still awaiting its link -- and the notice has to
    // come back with the poll still under it.
    ensureReadinessPolling();
  }

  // A target is waiting on Cloudflare exactly when it has a published link that
  // has not answered a readiness probe yet. The poll keys off this alone; the
  // notice reads it too but additionally hides while a disable is in flight
  // (see renderTarget), so the notice can never appear without a poll.
  function isAwaitingLink(state) {
    return state.loaded && state.enabled && !state.isLive && !!state.url;
  }

  function loadTarget(service) {
    var state = stateFor(service);
    if (state.loaded) {
      renderTarget();
      return;
    }
    state.failed = false;
    renderTarget();
    requestWithErrorCheck(sharingUrlFor(service), { method: 'GET' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var policyEmails = (data && data.policy && data.policy.emails) || [];
        state.enabled = !!(data && data.enabled);
        state.url = (data && data.url) || '';
        state.emails = policyEmails.filter(function (email) { return email !== ownerEmail; });
        // An already-published link is assumed live: the provisioning wait only
        // applies to a hostname this session just created.
        state.isLive = state.enabled;
        state.loaded = true;
        if (service === currentTarget) renderTarget();
      })
      .catch(function (error) {
        state.failed = true;
        if (service === currentTarget) {
          renderTarget();
          showError('Could not load sharing status: ' + error.message + ' -- select this target again to retry.');
        }
      });
  }

  function selectTarget(service) {
    if (service === currentTarget) {
      // Re-clicking the selected target is the retry affordance for a status
      // read that failed -- a workspace whose only target is the whole machine
      // has nothing else to click. The email input is left alone: the user may
      // have typed into it while the read was failing.
      if (!stateFor(service).failed) return;
      clearError();
      loadTarget(service);
      return;
    }
    clearError();
    stopReadinessPolling();
    // The copy confirmation belongs to the link that was on screen; carrying a
    // green check over to a different target would claim its link was copied.
    cancelCopyConfirmation();
    currentTarget = service;
    var input = el('ws-share-new-email');
    if (input) input.value = '';
    loadTarget(service);
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-share-target]'), function (button) {
    button.addEventListener('click', function () { selectTarget(button.dataset.shareTarget); });
  });

  // Remove buttons are rebuilt on every render, so the handler is delegated.
  // Named (not inline) so teardown can detach it again.
  function onRemoveEmailClick(event) {
    var button = event.target.closest ? event.target.closest('[data-remove-email]') : null;
    if (!button) return;
    removeEmail(button.dataset.removeEmail);
  }
  document.addEventListener('click', onRemoveEmailClick);

  // Release everything that outlives the page body when the shell swaps this
  // page out in place (the overlay panel never fires this -- its iframe is
  // destroyed wholesale -- so the listener simply never runs there).
  window.addEventListener('minds:page-teardown', function () {
    isPageTornDown = true;
    stopReadinessPolling();
    document.removeEventListener('click', onRemoveEmailClick);
  }, { once: true });

  function removeEmail(email) {
    var state = stateFor(currentTarget);
    state.emails = state.emails.filter(function (existing) { return existing !== email; });
    renderAcl();
    // While sharing is off the list is only staged locally -- "Enable sharing"
    // publishes it. Once it is on, every change is a live policy replace.
    if (state.enabled) persistEmails();
  }

  window.wsShareAddEmail = function () {
    var input = el('ws-share-new-email');
    if (!input) return;
    var email = (input.value || '').trim();
    if (!email) return;
    var state = stateFor(currentTarget);
    if (email !== ownerEmail && state.emails.indexOf(email) < 0) state.emails.push(email);
    input.value = '';
    clearError();
    renderAcl();
    if (state.enabled) persistEmails();
  };

  function persistEmails() {
    clearError();
    var service = currentTarget;
    startPending(service, 'emails');
    requestWithErrorCheck(sharingUrlFor(service), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: finalEmails() }),
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var state = stateFor(service);
        markKnown(state);
        state.enabled = true;
        if (data && data.url) state.url = data.url;
        endPending(service);
        if (service === currentTarget) renderTarget();
      })
      .catch(function (error) {
        endPending(service);
        if (service === currentTarget) showError('Could not update who this is shared with: ' + error.message);
      });
  }

  window.wsShareEnable = function () {
    clearError();
    var service = currentTarget;
    startPending(service, 'enable');
    requestWithErrorCheck(sharingUrlFor(service), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: finalEmails() }),
    })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var state = stateFor(service);
        markKnown(state);
        state.enabled = true;
        state.url = (data && data.url) || '';
        state.isLive = false;
        endPending(service);
        // renderTarget arms the readiness poll for whichever target is on
        // screen; if the user switched away mid-request, selecting this one
        // again picks the poll back up.
        if (service === currentTarget) renderTarget();
      })
      .catch(function (error) {
        endPending(service);
        if (service === currentTarget) showError('Could not enable sharing: ' + error.message);
      });
  };

  // The readiness poll is NOT called off up front: a DELETE that fails leaves
  // the target still enabled and still awaiting its link, and a poll stopped
  // ahead of that failure would strand the "not live yet" notice with nothing
  // behind it. renderTarget stops the poll on success, when the state actually
  // says the link is gone.
  window.wsShareDisable = function () {
    clearError();
    var service = currentTarget;
    startPending(service, 'disable');
    requestWithErrorCheck(sharingUrlFor(service), { method: 'DELETE' })
      .then(function () {
        var state = stateFor(service);
        markKnown(state);
        state.enabled = false;
        state.url = '';
        state.isLive = false;
        endPending(service);
        if (service === currentTarget) renderTarget();
      })
      .catch(function (error) {
        endPending(service);
        if (service === currentTarget) showError('Could not disable sharing: ' + error.message);
      });
  };

  // The write can be refused (clipboard permission, or an unfocused document --
  // this pane lives in an overlay view, so that is a real case). Say so rather
  // than leaving the user to paste whatever was on the clipboard before.
  // Confirm a copy by flashing the link pill green, the same way the
  // inspiration flow confirms its copy: the clipboard gives no feedback of its
  // own, and a link that looks unchanged after a click reads as a dead button.
  // Set through the theme's success variables inline so it stays theme-aware
  // and beats the pill's own hover colors.
  //
  // Three things say it at once, because a copy leaves no trace anywhere else:
  // the pill flashes green, its copy glyph becomes a green check, and a
  // "Copied" bubble appears above it. The bubble is drawn here rather than
  // through the hover-tooltip module, which is driven by hover intent and
  // hides itself on the very click that would trigger this.
  var COPY_FLASH_MS = 1200;
  var copyFlashTimer = null;

  function showCopyConfirmation(isShown) {
    var pill = el('ws-share-url-btn');
    if (pill) {
      pill.style.borderColor = isShown ? 'var(--c-success)' : '';
      pill.style.backgroundColor = isShown ? 'var(--c-success-surface)' : '';
    }
    var copyIcon = el('ws-share-copy-icon');
    if (copyIcon) copyIcon.classList.toggle('hidden', isShown);
    var copiedIcon = el('ws-share-copied-icon');
    if (copiedIcon) copiedIcon.classList.toggle('hidden', !isShown);
    var bubble = el('ws-share-copied-bubble');
    if (bubble) bubble.classList.toggle('hidden', !isShown);
  }

  function cancelCopyConfirmation() {
    if (copyFlashTimer !== null) {
      clearTimeout(copyFlashTimer);
      copyFlashTimer = null;
    }
    showCopyConfirmation(false);
  }

  function flashCopied() {
    showCopyConfirmation(true);
    // A second copy before the first has faded restarts the beat rather than
    // letting the earlier timer cut it short.
    if (copyFlashTimer !== null) clearTimeout(copyFlashTimer);
    copyFlashTimer = setTimeout(function () {
      copyFlashTimer = null;
      showCopyConfirmation(false);
    }, COPY_FLASH_MS);
  }

  window.wsShareCopyUrl = function () {
    var state = stateFor(currentTarget);
    if (!state.url) return;
    clearError();
    navigator.clipboard.writeText(state.url).then(flashCopied).catch(function (error) {
      showError('Could not copy the link: ' + error.message);
    });
  };

  // Cloudflare publishes a (re)created hostname at its edge a minute or two
  // after the tunnel accepts it, so a link opened immediately after enabling
  // 404s. Poll fast at first, then back off, and stop warning at the deadline
  // rather than pretending success forever.
  var READINESS_FAST_INTERVAL_MS = 2000;
  var READINESS_SLOW_INTERVAL_MS = 5000;
  var READINESS_FAST_PHASE_MS = 30 * 1000;
  var READINESS_DEADLINE_MS = 5 * 60 * 1000;

  function stopReadinessPolling() {
    if (readinessTimer !== null) {
      clearTimeout(readinessTimer);
      readinessTimer = null;
    }
    pollingService = null;
  }

  // Keep exactly one poll running, for the target on screen, for as long as
  // that target claims a link that is not live yet. Called from renderTarget,
  // so re-selecting a target enabled earlier in this session resumes its poll
  // instead of leaving the "not live yet" notice up forever.
  function ensureReadinessPolling() {
    var state = stateFor(currentTarget);
    if (!isAwaitingLink(state)) {
      stopReadinessPolling();
      return;
    }
    if (pollingService === currentTarget) return;
    startReadinessPolling(currentTarget, state.url);
  }

  function startReadinessPolling(service, url) {
    stopReadinessPolling();
    if (!url || isPageTornDown) return;
    pollingService = service;
    var startedAt = Date.now();

    function poll() {
      readinessTimer = null;
      if (isSuperseded()) return;
      var elapsed = Date.now() - startedAt;
      if (elapsed > READINESS_DEADLINE_MS) {
        markLive(service);
        return;
      }
      fetch(sharingUrlFor(service) + '/readiness?url=' + encodeURIComponent(url))
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data && data.ready) {
            markLive(service);
            return;
          }
          schedule(elapsed);
        })
        .catch(function () { schedule(elapsed); });
    }

    // A fetch can land after this poll was called off (page torn down, target
    // switched, sharing disabled); it must not re-arm the timer then.
    function isSuperseded() {
      return isPageTornDown || pollingService !== service;
    }

    function schedule(elapsed) {
      if (isSuperseded()) return;
      var interval = elapsed < READINESS_FAST_PHASE_MS ? READINESS_FAST_INTERVAL_MS : READINESS_SLOW_INTERVAL_MS;
      readinessTimer = setTimeout(poll, interval);
    }

    schedule(0);
  }

  function markLive(service) {
    stateFor(service).isLive = true;
    if (service === currentTarget) renderTarget();
  }

  loadTarget(currentTarget);
})();
