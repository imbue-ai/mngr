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
// Sharing is machine-level: one share per machine with a single grants
// document, onto which the pane maps its per-target view ("Whole machine" or
// one app). See the Share machine pane section below for the full model.
//
// The ACL is rebuilt with DOM methods (never innerHTML) so a crafted email
// cannot inject script.

(function () {
  'use strict';

  // The full-page auth flow, which the shell replaces with its sign-in modal.
  // A completed sign-in lands the shell on the workspace list; the panel's
  // own URL is not a content path, so it cannot be the return target.
  var AUTH_LOGIN_PATH = '/auth/login';
  var SIGNIN_RETURN_PATH = '/';

  // -- Panel chrome ---------------------------------------------------------

  // Dismissal: hosted in the chrome shell the panel is an overlay-layer
  // iframe, so it must ask the shell to tear the overlay down; as a plain
  // page there is nothing to close,
  // so fall back to the workspace. The ``/goto/`` bridge is served by the mngr
  // forward plugin, NOT by minds' own origin, so it needs the plugin origin the
  // page carries on its body (same read as sidebar.js); without one there is
  // no workspace URL to build, so land on the workspace list.
  window.dismissWorkspaceOptions = function () {
    var backdrop = document.getElementById('ws-options-backdrop');
    // The /goto/ route is host-keyed; the agent id is only the last resort
    // when no host coordinate is known (same degrade as the landing rows).
    var workspaceId = backdrop ? (backdrop.dataset.hostId || backdrop.dataset.agentId) : '';
    if (window.minds && window.minds.closeModal) {
      window.minds.closeModal();
      return;
    }
    var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';
    window.location.href = workspaceId && mngrForwardOrigin
      ? mngrForwardOrigin + '/goto/' + encodeURIComponent(workspaceId) + '/'
      : '/';
  };

  var backdropEl = document.getElementById('ws-options-backdrop');
  if (backdropEl) {
    backdropEl.addEventListener('click', function (event) {
      if (event.target === backdropEl) window.dismissWorkspaceOptions();
    });

    // No link inside the panel may navigate the overlay iframe: a same-origin
    // href (the backups page's "View all backups", the Associate prompt's
    // sign-in link) would load a full page inside the modal iframe and strand
    // the app there. Hand those to the shell instead and dismiss. Only
    // present when hosted in the overlay layer -- the standalone settings
    // page navigates normally.
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
      // the panel's own URL to the shell would be a spurious navigation.
      if (target.origin !== window.location.origin) return;
      if (target.pathname === window.location.pathname && target.search === window.location.search) return;
      event.preventDefault();
      // Signing in is a modal in the chrome shell, so the Associate prompt's
      // "Sign in or create an account" opens that rather than sending the whole
      // app off to the full-page auth flow -- the panel is where the user is
      // working, and the full page is only the standalone fallback.
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
  //
  // Sharing is machine-level under the self-hosted relay: ONE share per
  // machine (one tunnel, one domain, one grants document), where the grants
  // document carries a workspace-level allow-list (admits every service) plus
  // optional per-service lists (each admits only that one service's origin).
  // The pane keeps the per-target mental model -- pick "Whole machine" or one
  // app, manage who can reach it, get its link -- and maps each target onto
  // one scope of the grants document:
  //
  //   Whole machine  <->  the ``workspace`` scope   (link: the machine domain)
  //   app <name>     <->  ``services.<name>``       (link: <name>.<machine domain>)
  //
  // A target is "on" exactly when its scope grants anyone (the owner is always
  // written into an enabled scope, so enabling with nobody else added still
  // works). The machine's share exists while ANY scope is on: enabling the
  // first target provisions the share (relay token, cert, tunnel -- the slow
  // path the provisioning notice narrates); disabling the last one deletes it.
  // Everything in between is a grants-document rewrite the workspace's
  // gateway picks up per-request, with no tunnel restart.
  //
  // One GET returns the whole document, so every target's state is known at
  // once (no per-target status subprocesses). Writes PUT the whole document
  // too, so they are queued and serialized: two targets edited back-to-back
  // must not race each other with full-document replaces.

  var configEl = document.getElementById('ws-share-config');
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);
  var agentId = config.agentId;
  // Machine-sharing endpoints are keyed by host id; the agent id is the
  // fallback when the host coordinate is unknown (the read then reports
  // "not shared", matching the pre-discovery state of the workspace).
  var shareHostId = config.hostId || agentId;
  var wholeService = config.wholeService;
  // Per-app-service origin label (``<name>-<rand>``), keyed by service name. A
  // per-app share link is a real origin (``<label>.<machine domain>``), so its
  // hostname is the service's label, not its name. A service missing here
  // (a legacy row with no label) falls back to its own name in targetUrl.
  var serviceLabels = config.serviceLabels || {};
  var ownerEmail = config.accountEmail || '';
  var shareApiBase = '/api/v1/machines/' + encodeURIComponent(shareHostId) + '/sharing';

  // Machine-level share state, loaded once for every target:
  //   loaded  -- the grants document came back (targets are renderable)
  //   failed  -- the read never landed; deliberately NOT ``loaded`` (a status
  //              read that never landed must not masquerade as "sharing is
  //              off", or enabling from that pane would replace an access
  //              policy nobody ever saw)
  //   enabled -- the machine share exists (some scope grants someone)
  //   url     -- the machine's public URL (per-target links derive from it)
  //   isLive  -- the shared hostname has answered a readiness probe
  var machine = { loaded: false, failed: false, enabled: false, url: '', isLive: false };

  // Per-target scope state: ``enabled`` (the scope grants someone) and
  // ``entries`` (emails and bare domains, owner excluded -- the owner is
  // implicitly first and never removable). While a target is off its entries
  // are only staged locally; enabling publishes them.
  var stateByTarget = {};

  // Grants scopes for services that are NOT rendered as targets (a service
  // granted from outside this pane, or one no longer registered). Preserved
  // verbatim through every write so a full-document replace cannot silently
  // drop access that was never on screen.
  var extraServiceGrants = {};

  var currentTarget = config.selectedTarget || wholeService;
  var readinessTimer = null;
  // Which target ``readinessTimer`` belongs to, so re-rendering the same
  // target does not restart its clock (null whenever no poll is armed).
  var pollingService = null;
  // The full-page twin is in the shell's in-place swap set, so its document
  // outlives the page: without this, each visit would leave another live
  // delegated handler (whose stale closure rewrites the ACL and re-PUTs the
  // grants document) plus a readiness poll still hitting the server.
  var isPageTornDown = false;

  // Every rendered target, so document builds cover targets that are not
  // currently selected (their scopes must survive a write made from another
  // target's pane).
  var knownTargets = [];
  Array.prototype.forEach.call(document.querySelectorAll('[data-share-target]'), function (button) {
    if (knownTargets.indexOf(button.dataset.shareTarget) < 0) knownTargets.push(button.dataset.shareTarget);
  });
  if (knownTargets.indexOf(wholeService) < 0) knownTargets.push(wholeService);

  // A ?target= naming something that is not rendered (e.g. a legacy link to a
  // service the DNS-safe filter now excludes) must not become the selection:
  // document builds iterate only knownTargets, so edits staged on a phantom
  // target would be silently dropped from every write.
  if (knownTargets.indexOf(currentTarget) < 0) currentTarget = wholeService;

  function el(id) { return document.getElementById(id); }

  function targetLabel(service) {
    return service === wholeService ? 'Whole machine' : service;
  }

  function targetSubtitle(service) {
    return service === wholeService
      ? 'Give access to everything in this machine, including every app.'
      : 'Give access only to this app on its own.';
  }

  function stateFor(service) {
    if (!stateByTarget[service]) {
      stateByTarget[service] = { enabled: false, entries: [] };
    }
    return stateByTarget[service];
  }

  function showError(message) {
    var errorEl = el('ws-share-error');
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
    setRetryShown(false);
  }

  // A LOAD failure gets its own Try again affordance: the editor stays locked
  // until a clean status read lands, and nothing else on screen re-triggers
  // one. Action failures (enable, disable, list edits) never show it -- their
  // retry is redoing the action, whose control is still right there.
  function showLoadFailure(message) {
    showError(message);
    setRetryShown(true);
  }

  function setRetryShown(isShown) {
    var retryRow = el('ws-share-retry-row');
    if (retryRow) retryRow.classList.toggle('hidden', !isShown);
  }

  function clearError() {
    var errorEl = el('ws-share-error');
    if (errorEl) errorEl.classList.add('hidden');
    setRetryShown(false);
  }

  window.wsShareRetryLoad = function () {
    clearError();
    loadMachine();
  };

  // ``fetch`` only rejects on network failure -- a 4xx/5xx response is a
  // successful Promise. Wrap it so callers treat transport errors and
  // server-side errors uniformly.
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

  function isEmailEntry(entry) {
    return entry.indexOf('@') >= 0;
  }

  function createAclRow(entry, isOwner) {
    var row = document.createElement('div');
    row.className = 'flex items-center justify-between gap-2 rounded-md border border-subtle bg-fill-subtle px-3 py-2';

    var label = document.createElement('span');
    label.className = 'type-body text-primary truncate';
    label.textContent = entry;
    if (isOwner) {
      var suffix = document.createElement('span');
      suffix.className = 'text-tertiary';
      suffix.textContent = ' (you)';
      label.appendChild(suffix);
    } else if (!isEmailEntry(entry)) {
      // A bare entry grants a whole email domain; say so on the row rather
      // than leaving it to read as a typo of an address.
      var domainSuffix = document.createElement('span');
      domainSuffix.className = 'text-tertiary';
      domainSuffix.textContent = ' (anyone at this domain)';
      label.appendChild(domainSuffix);
    }
    row.appendChild(label);

    if (!isOwner) {
      var removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'shrink-0 inline-flex h-6 w-6 items-center justify-center rounded-md ' +
        'text-tertiary hover:bg-fill-hover hover:text-important cursor-pointer transition-colors';
      // aria-label carries the whole affordance: tooltip_triggers.js binds
      // ``data-tooltip`` once at load, so a row built here -- long after that
      // pass -- could never get one.
      removeBtn.setAttribute('aria-label', 'Remove ' + entry);
      removeBtn.dataset.removeEmail = entry;
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
    state.entries.forEach(function (entry) {
      listEl.appendChild(createAclRow(entry, false));
    });
  }

  // One scope of the grants document, from a target's staged entries. The
  // owner's email is always written into an enabled scope: the gateway has no
  // implicit owner bypass, and the owner losing access to their own machine's
  // share would be surprising.
  function grantListFor(service) {
    var emails = ownerEmail ? [ownerEmail] : [];
    var emailDomains = [];
    stateFor(service).entries.forEach(function (entry) {
      if (isEmailEntry(entry)) {
        if (emails.indexOf(entry) < 0) emails.push(entry);
      } else if (emailDomains.indexOf(entry) < 0) {
        emailDomains.push(entry);
      }
    });
    return { emails: emails, email_domains: emailDomains };
  }

  // The full grants document as it should be after this write. ``overrides``
  // maps a target to true/false to enable/disable it as part of the same
  // write (the scope flags flip only once the server confirms).
  function buildGrantsDocument(overrides) {
    overrides = overrides || {};
    function isOn(service) {
      return Object.prototype.hasOwnProperty.call(overrides, service) ? overrides[service] : stateFor(service).enabled;
    }
    var doc = { workspace: { emails: [], email_domains: [] }, services: {} };
    knownTargets.forEach(function (service) {
      if (!isOn(service)) return;
      if (service === wholeService) doc.workspace = grantListFor(service);
      else doc.services[service] = grantListFor(service);
    });
    Object.keys(extraServiceGrants).forEach(function (name) {
      doc.services[name] = extraServiceGrants[name];
    });
    return doc;
  }

  function documentGrantsAnyone(doc) {
    if (doc.workspace.emails.length || doc.workspace.email_domains.length) return true;
    return Object.keys(doc.services).some(function (name) {
      var scope = doc.services[name];
      return (scope.emails || []).length > 0 || (scope.email_domains || []).length > 0;
    });
  }

  function scopeEntries(scope) {
    var entries = [];
    ((scope && scope.emails) || []).forEach(function (email) {
      if (email !== ownerEmail) entries.push(email);
    });
    ((scope && scope.email_domains) || []).forEach(function (domain) {
      entries.push(domain);
    });
    return entries;
  }

  function scopeGrantsAnyone(scope) {
    return !!scope && ((scope.emails || []).length > 0 || (scope.email_domains || []).length > 0);
  }

  // Adopt a sharing document from the server (a GET, or any write's
  // response) as the authoritative state. Scopes absent from the document are
  // off, but their locally staged entries are kept: staging happens before a
  // scope exists server-side at all.
  function syncFromDocument(data) {
    machine.enabled = !!(data && data.enabled);
    machine.url = (data && data.url) || '';
    var grants = (data && data.grants) || {};
    var services = grants.services || {};
    knownTargets.forEach(function (service) {
      var scope = service === wholeService ? grants.workspace : services[service];
      var state = stateFor(service);
      if (scopeGrantsAnyone(scope)) {
        state.enabled = true;
        state.entries = scopeEntries(scope);
      } else {
        state.enabled = false;
      }
    });
    extraServiceGrants = {};
    Object.keys(services).forEach(function (name) {
      if (knownTargets.indexOf(name) >= 0) return;
      if (!scopeGrantsAnyone(services[name])) return;
      extraServiceGrants[name] = {
        emails: (services[name].emails || []).slice(),
        email_domains: (services[name].email_domains || []).slice(),
      };
    });
    machine.loaded = true;
    machine.failed = false;
  }

  // The link each target hands out is a service origin: the service's label in
  // front of the machine domain. This holds for the whole machine too -- its
  // link is the SHELL's (system_interface) label origin, because the bare
  // machine domain does not route on a share (only explicit ``<label>.<domain>``
  // origins are claimed on the relay and served).
  // ``machine.url`` supplies the base host (the bare machine domain / host
  // coordinate); the label is prefixed onto it. Derived, never stored -- a
  // service registered while shared is reachable immediately.
  function targetUrl(service) {
    if (!machine.url) return '';
    var host;
    try {
      host = new URL(machine.url).host;
    } catch (_) {
      return '';
    }
    var label = serviceLabels[service];
    if (service === wholeService) {
      // Fall back to the bare machine URL only if the shell label is somehow
      // not known yet -- better a link that may not route than a broken one
      // built from the shell's NAME (which is not its routable label).
      return label ? 'https://' + label + '.' + host + '/' : machine.url;
    }
    return 'https://' + (label || service) + '.' + host + '/';
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
  // held in one flag for the pane, so every target shows its own truth and
  // coming back to one with a write still running finds it still busy.
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

  // Writes replace the whole grants document, so they are strictly
  // serialized: each builds its request body only when its turn comes, on top
  // of whatever the previous write left behind. Without this, two targets
  // edited back-to-back would race full-document replaces and the loser's
  // change would vanish.
  var writeChain = Promise.resolve();

  function enqueueWrite(makeRequest) {
    var result = writeChain.then(makeRequest, makeRequest);
    // The chain itself swallows failures (each write reports its own);
    // returning ``result`` keeps them visible to the write's own caller.
    writeChain = result.then(function () {}, function () {});
    return result;
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
    // An access-list edit changes the list rather than any one button, so its
    // wait goes to the status line too.
    else setBusyLine(kind === 'emails', 'Updating who can open this link...');
    // A pane whose status never loaded cannot be edited either (see below).
    setEditable(machine.loaded && !kind);
  }

  // While the document is unloaded (or its read failed) no control that would
  // write a policy is offered: an address staged against a document nobody
  // ever saw could replace grants invisibly.
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
    if (loadingEl) loadingEl.classList.toggle('hidden', machine.loaded || machine.failed);
    // A disable takes the link away the moment it is asked for, rather than
    // leaving a link on screen that is already being revoked. Neither row shows
    // while it runs -- the status line below carries the wait, and the Enable
    // button reappears when the server confirms the link is gone.
    var isDisabling = pendingByTarget[currentTarget] === 'disable';
    var enableRow = el('ws-share-enable-row');
    if (enableRow) {
      var showEnable = machine.loaded && !state.enabled && !isDisabling;
      enableRow.classList.toggle('hidden', !showEnable);
      enableRow.classList.toggle('flex', showEnable);
    }
    var urlRow = el('ws-share-url-row');
    if (urlRow) {
      var showUrl = machine.loaded && state.enabled && !isDisabling;
      urlRow.classList.toggle('hidden', !showUrl);
      // The row's layout classes only apply once it is not hidden; ``hidden``
      // and ``flex`` would otherwise fight (see Badge.jinja's note).
      urlRow.classList.toggle('flex', showUrl);
    }
    var urlEl = el('ws-share-url');
    if (urlEl) urlEl.textContent = targetUrl(currentTarget) || '';
    var provisioningEl = el('ws-share-provisioning');
    // Also gone while a disable runs: the link this notice is about has already
    // come off screen, so explaining that the share is still provisioning
    // describes something the user can no longer see.
    if (provisioningEl) provisioningEl.classList.toggle('hidden', !isAwaitingLink(currentTarget) || isDisabling);
    renderAcl();
    applyPending();
    // A visible notice always has a poll behind it, or it would never clear.
    // The reverse is allowed on purpose: a disable in flight hides the notice
    // but leaves the poll running, because a delete that fails leaves the
    // target still enabled and still awaiting its link -- and the notice has to
    // come back with the poll still under it.
    ensureReadinessPolling();
  }

  // A target is waiting on provisioning exactly when it has a link that has
  // not answered a readiness probe yet. Readiness is machine-level (one
  // tunnel, one cert), so every enabled target waits and clears together.
  function isAwaitingLink(service) {
    return machine.loaded && stateFor(service).enabled && !machine.isLive && !!machine.url;
  }

  function loadMachine() {
    machine.failed = false;
    renderTarget();
    requestWithErrorCheck(shareApiBase, { method: 'GET' })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        // grants: null means the machine is shared but the grants read never
        // landed. That is a failed read, not an empty policy -- adopting it
        // would render every grantee as revoked and let an edit replace a
        // policy nobody ever saw. Say what still works (the share itself) so
        // a management-view hiccup does not read as a broken share.
        if (data && data.enabled && !data.grants) {
          machine.failed = true;
          renderTarget();
          showLoadFailure(
            'This machine is shared and everyone granted access still has it, but the list of ' +
            'who that is could not be loaded, so it cannot be edited right now.'
          );
          return;
        }
        syncFromDocument(data);
        // An already-published link is assumed live: the provisioning wait
        // only applies to a share this session just created.
        machine.isLive = machine.enabled;
        renderTarget();
      })
      .catch(function (error) {
        machine.failed = true;
        renderTarget();
        showLoadFailure('Could not load sharing status: ' + error.message);
      });
  }

  function selectTarget(service) {
    if (service === currentTarget) {
      // Re-clicking the selected target is the retry affordance for a status
      // read that failed -- a machine whose only target is the whole machine
      // has nothing else to click. The input is left alone: the user may
      // have typed into it while the read was failing.
      if (!machine.failed) return;
      clearError();
      loadMachine();
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
    updateAddButtonEmphasis();
    if (machine.failed) {
      loadMachine();
      return;
    }
    renderTarget();
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

  function removeEmail(entry) {
    var state = stateFor(currentTarget);
    state.entries = state.entries.filter(function (existing) { return existing !== entry; });
    renderAcl();
    // While sharing is off the list is only staged locally -- "Enable sharing"
    // publishes it. Once it is on, every change is a live grants replace.
    if (state.enabled) persistEntries();
  }

  // What the add-email input currently holds, trimmed ('' when blank).
  function pendingEmailText() {
    var input = el('ws-share-new-email');
    return input ? (input.value || '').trim() : '';
  }

  // While the input holds un-added text the Add button wears the prominent
  // (primary) variant so it reads as the obvious next action; empty goes back
  // to the quiet secondary. The two class sets ride on the button's data
  // attributes (rendered from the template's variant recipes).
  function updateAddButtonEmphasis() {
    var addBtn = el('ws-share-add-btn');
    if (!addBtn) return;
    var secondaryClasses = (addBtn.dataset.variantSecondary || '').split(/\s+/).filter(Boolean);
    var primaryClasses = (addBtn.dataset.variantPrimary || '').split(/\s+/).filter(Boolean);
    if (!secondaryClasses.length || !primaryClasses.length) return;
    var activeClasses = pendingEmailText() ? primaryClasses : secondaryClasses;
    secondaryClasses.concat(primaryClasses).forEach(function (cls) { addBtn.classList.remove(cls); });
    activeClasses.forEach(function (cls) { addBtn.classList.add(cls); });
  }

  var addEmailInput = el('ws-share-new-email');
  if (addEmailInput) addEmailInput.addEventListener('input', updateAddButtonEmphasis);

  window.wsShareAddEmail = function () {
    var input = el('ws-share-new-email');
    if (!input) return;
    var entry = (input.value || '').trim();
    if (!entry) return;
    var state = stateFor(currentTarget);
    if (entry !== ownerEmail && state.entries.indexOf(entry) < 0) state.entries.push(entry);
    input.value = '';
    updateAddButtonEmphasis();
    clearError();
    renderAcl();
    if (state.enabled) persistEntries();
  };

  function putGrantsDocument(overrides) {
    return requestWithErrorCheck(shareApiBase, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildGrantsDocument(overrides)),
    }).then(function (response) { return response.json(); });
  }

  function persistEntries() {
    clearError();
    var service = currentTarget;
    startPending(service, 'emails');
    enqueueWrite(function () { return putGrantsDocument({}); })
      .then(function (data) {
        syncFromDocument(data);
        endPending(service);
        if (service === currentTarget) renderTarget();
      })
      .catch(function (error) {
        endPending(service);
        if (service === currentTarget) showError('Could not update who this is shared with: ' + error.message);
      });
  }

  window.wsShareEnable = function () {
    // Un-added text left in the email box must not be silently dropped by the
    // publish (that is how a share once went out without its intended
    // grantee) -- and auto-adding it would be guessing. Block and say which.
    var residualEntry = pendingEmailText();
    if (residualEntry) {
      showError("Either click 'Add' to share with " + residualEntry + ", or clear the box first.");
      return;
    }
    clearError();
    var service = currentTarget;
    startPending(service, 'enable');
    enqueueWrite(function () { return putGrantsDocument(makeOverride(service, true)); })
      .then(function (data) {
        // Whether this enable created the machine share (slow: relay token,
        // cert, tunnel) or joined an existing one (instant: the wildcard cert
        // and vhost already cover every service origin) decides whether the
        // link needs the provisioning wait.
        var isFirstShare = !machine.enabled;
        syncFromDocument(data);
        if (isFirstShare) machine.isLive = false;
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

  function makeOverride(service, isOn) {
    var overrides = {};
    overrides[service] = isOn;
    return overrides;
  }

  // The readiness poll is NOT called off up front: a delete that fails leaves
  // the target still enabled and still awaiting its link, and a poll stopped
  // ahead of that failure would strand the "not live yet" notice with nothing
  // behind it. renderTarget stops the poll on success, when the state actually
  // says the link is gone.
  window.wsShareDisable = function () {
    clearError();
    var service = currentTarget;
    startPending(service, 'disable');
    enqueueWrite(function () {
      // Turning off the last target unshares the machine (the relay token
      // dies and live viewers are cut); otherwise the scope is dropped from
      // the grants document and every other target stays reachable.
      var remaining = buildGrantsDocument(makeOverride(service, false));
      if (!documentGrantsAnyone(remaining)) {
        return requestWithErrorCheck(shareApiBase, { method: 'DELETE' })
          .then(function () { return null; });
      }
      return requestWithErrorCheck(shareApiBase, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(remaining),
      }).then(function (response) { return response.json(); });
    })
      .then(function (data) {
        if (data === null) {
          machine.enabled = false;
          machine.url = '';
          machine.isLive = false;
          machine.loaded = true;
          machine.failed = false;
          knownTargets.forEach(function (target) { stateFor(target).enabled = false; });
          extraServiceGrants = {};
        } else {
          syncFromDocument(data);
        }
        // The scope is off either way; its staged entries stay for a re-enable.
        stateFor(service).enabled = false;
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
  // template flow confirms its copy: the clipboard gives no feedback of its
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
    var url = targetUrl(currentTarget);
    if (!url) return;
    clearError();
    navigator.clipboard.writeText(url).then(flashCopied).catch(function (error) {
      showError('Could not copy the link: ' + error.message);
    });
  };

  // A freshly-created share takes a little while to come up end to end: the
  // workspace generates its key, the connector completes the ACME certificate,
  // and the tunnel dials the relay. Poll fast at first, then back off, and
  // stop warning at the deadline rather than pretending success forever.
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
    if (!isAwaitingLink(currentTarget)) {
      stopReadinessPolling();
      return;
    }
    if (pollingService === currentTarget) return;
    startReadinessPolling(currentTarget);
  }

  function startReadinessPolling(service) {
    stopReadinessPolling();
    if (isPageTornDown) return;
    pollingService = service;
    var startedAt = Date.now();

    function poll() {
      readinessTimer = null;
      if (isSuperseded()) return;
      var elapsed = Date.now() - startedAt;
      if (elapsed > READINESS_DEADLINE_MS) {
        markLive();
        return;
      }
      fetch(shareApiBase + '/readiness')
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (data && data.ready) {
            markLive();
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

  // Readiness is machine-level: one tunnel and one certificate answer for
  // every target's link, so going live clears the notice everywhere at once.
  function markLive() {
    machine.isLive = true;
    renderTarget();
  }

  loadMachine();
})();
