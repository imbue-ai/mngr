// Sharing editor: rebuilds the ACL + heading via DOM methods (NOT innerHTML)
// so a crafted email cannot inject script. The page config is passed from
// Jinja as a JSON data island, not as template-interpolated JS.
(function () {
  var configEl = document.getElementById('sharing-config');
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);
  var agentId = config.agentId;
  var serviceName = config.serviceName;
  var wsName = config.wsName;
  var accountEmail = config.accountEmail;
  var proposedEmails = config.initialEmails || [];
  // ``mngr forward`` plugin's bare origin (e.g. ``http://localhost:8421``);
  // the workspace link below targets the plugin's ``/goto/<agent>/`` route.
  var mngrForwardOrigin = (document.body && document.body.dataset.mngrForwardOrigin) || '';

  function setHeading(isEnabled) {
    var h = document.getElementById('page-heading');
    if (!h) return;
    // The modal heading (data-plain-links) renders names as plain text: a
    // link there would navigate the overlay iframe to a full page and strand
    // the app inside the modal. The full page keeps its links.
    var plainLinks = h.dataset.plainLinks === 'true';
    h.textContent = '';
    h.appendChild(document.createTextNode(isEnabled ? '' : 'Share '));

    var codeEl = document.createElement('code');
    codeEl.className = 'code-pill';
    codeEl.textContent = serviceName;
    h.appendChild(codeEl);

    h.appendChild(document.createTextNode(isEnabled ? ' shared in ' : ' in '));

    if (plainLinks) {
      h.appendChild(document.createTextNode(wsName));
    } else {
      var link = document.createElement('a');
      link.href = mngrForwardOrigin + '/goto/' + agentId + '/';
      link.className = 'text-accent hover:underline';
      link.textContent = wsName;
      h.appendChild(link);
    }

    if (accountEmail) {
      h.appendChild(document.createTextNode(' ('));
      if (plainLinks) {
        h.appendChild(document.createTextNode(accountEmail));
      } else {
        var acctLink = document.createElement('a');
        acctLink.href = '/accounts';
        acctLink.className = 'text-accent hover:underline';
        acctLink.textContent = accountEmail;
        h.appendChild(acctLink);
      }
      h.appendChild(document.createTextNode(')'));
    }

    if (!isEnabled) h.appendChild(document.createTextNode('?'));
  }

  // Three-state ACL. Every email lives in textContent/dataset, never in HTML.
  var existing = [];
  var added = [];
  var removed = [];

  function createAclRow(email, variant) {
    var base = 'flex items-center justify-between px-3 py-2 border rounded-md my-1 ';
    var rowCls = {
      existing: 'bg-surface-primary border-default',
      added:    'bg-success/12 border-success/30',
      removed:  'bg-important/12 border-important/30 line-through',
    }[variant];
    var row = document.createElement('div');
    row.className = base + rowCls;

    var left = document.createElement('span');
    if (variant === 'added' || variant === 'removed') {
      var prefix = document.createElement('span');
      prefix.className = 'font-semibold mr-1.5 ' + (variant === 'added' ? 'text-success' : 'text-important');
      prefix.textContent = variant === 'added' ? '+' : '−';
      left.appendChild(prefix);
    }
    var emailEl = document.createElement('span');
    emailEl.className = 'type-body ' + (variant === 'removed' ? 'text-tertiary' : 'text-primary');
    emailEl.textContent = email;
    left.appendChild(emailEl);
    row.appendChild(left);

    var btn = document.createElement('button');
    btn.className = 'bg-transparent border-none cursor-pointer text-tertiary type-heading px-1 hover:text-primary';
    btn.setAttribute('aria-label', 'Remove');
    btn.setAttribute('data-action',
      variant === 'added' ? 'unmark-added'
      : variant === 'removed' ? 'unmark-removed'
      : 'mark-removed');
    btn.dataset.email = email;
    btn.innerHTML = '&times;';
    row.appendChild(btn);
    return row;
  }

  var lastAclSignature = null;

  function renderACL() {
    // Skip the rebuild when nothing changed, so the background reconciles
    // (the initial status GET, the refresh after a save) never touch a
    // settled list.
    var signature = JSON.stringify([existing, added, removed]);
    if (signature === lastAclSignature) return;
    lastAclSignature = signature;
    var container = document.getElementById('email-list');
    container.textContent = '';
    var rowCount = 0;
    existing.forEach(function (e) {
      if (removed.indexOf(e) >= 0) return;
      container.appendChild(createAclRow(e, 'existing'));
      rowCount++;
    });
    added.forEach(function (e) {
      container.appendChild(createAclRow(e, 'added'));
      rowCount++;
    });
    removed.forEach(function (e) {
      container.appendChild(createAclRow(e, 'removed'));
      rowCount++;
    });
    if (rowCount === 0) {
      var empty = document.createElement('p');
      empty.className = 'type-body text-tertiary';
      empty.textContent = 'No one in the access list';
      container.appendChild(empty);
    }
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest('button[data-action]');
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    var email = btn.dataset.email;
    if (!action || !email) return;
    if (action === 'mark-removed') markRemoved(email);
    else if (action === 'unmark-added') unmarkAdded(email);
    else if (action === 'unmark-removed') unmarkRemoved(email);
  });

  window.addEmail = function () {
    var input = document.getElementById('new-email');
    var email = input.value.trim();
    if (!email) return;
    if (removed.indexOf(email) >= 0) {
      removed = removed.filter(function (e) { return e !== email; });
    } else if (existing.indexOf(email) < 0 && added.indexOf(email) < 0) {
      added.push(email);
    }
    input.value = '';
    renderACL();
  };

  function markRemoved(email) {
    if (removed.indexOf(email) < 0) removed.push(email);
    renderACL();
  }
  function unmarkAdded(email) {
    added = added.filter(function (e) { return e !== email; });
    renderACL();
  }
  function unmarkRemoved(email) {
    removed = removed.filter(function (e) { return e !== email; });
    renderACL();
  }

  function getFinalEmails() {
    var result = existing.filter(function (e) { return removed.indexOf(e) < 0; });
    return result.concat(added);
  }

  function setSubmitting(submitting) {
    // Everything stays visible while a save is in flight: the action buttons
    // stay in place (disabled) and the inline "Saving changes..." note shows,
    // so the editor never blanks or reflows around the request.
    var spinner = document.getElementById('submit-spinner');
    spinner.classList.toggle('hidden', !submitting);
    var inputs = document.querySelectorAll('input, button, select');
    inputs.forEach(function (el) { el.disabled = submitting; });
    var editor = document.getElementById('editor-content');
    editor.style.opacity = submitting ? '0.5' : '1';
    editor.style.pointerEvents = submitting ? 'none' : 'auto';
  }

  // Render a server-side error inline above the action buttons. Called
  // when the sharing endpoints return a non-2xx/non-3xx response with a
  // JSON body of shape ``{"error": "..."}``. Without this the previous
  // code redirected on any response (including 5xx soft failures), so
  // a failed share appeared as "the emails just disappeared" with no
  // indication that anything went wrong.
  function showError(message) {
    var existing = document.getElementById('sharing-error');
    if (existing) existing.remove();
    var box = document.createElement('div');
    box.id = 'sharing-error';
    box.className = 'mt-3 mb-1 px-3 py-2 rounded-md bg-important/12 border border-important/30 type-body text-important';
    box.textContent = message;
    var actions = document.getElementById('action-buttons');
    actions.parentNode.insertBefore(box, actions);
  }

  function clearError() {
    var existing = document.getElementById('sharing-error');
    if (existing) existing.remove();
  }

  // ``fetch`` only rejects on network failure -- a 4xx/5xx response is
  // a successful Promise. Wrap it so callers can treat both transport
  // errors and server-side errors uniformly.
  function requestWithErrorCheck(url, options) {
    return fetch(url, options).then(function (r) {
      if (r.ok) return r;
      return r.text().then(function (text) {
        var detail = text;
        try {
          // Route both the structural 422 contract and the handler's semantic
          // {error}/{detail} shapes through the shared normalizer.
          detail = window.normalizeApiError(JSON.parse(text)).message;
        } catch (_) { /* leave detail as raw text */ }
        var err = new Error(detail || ('HTTP ' + r.status));
        err.httpStatus = r.status;
        throw err;
      });
    });
  }

  var statusUrl = '/api/v1/workspaces/' + agentId + '/sharing/' + serviceName;

  window.submitUpdate = function () {
    clearError();
    setSubmitting(true);
    requestWithErrorCheck(statusUrl, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: getFinalEmails() }),
    })
      // Refresh the editor in place from the same status GET (never reload,
      // and never navigate to the full page URL): this script also runs
      // inside the sharing modal's overlay iframe, where a reload blanks the
      // popup and a navigation to /sharing/... would strand the app inside
      // the modal.
      .then(refreshFromServer)
      .then(function () { setSubmitting(false); })
      .catch(function (err) { showError('Could not save sharing changes: ' + err.message); setSubmitting(false); });
  };

  window.submitDisable = function () {
    clearError();
    setSubmitting(true);
    requestWithErrorCheck(statusUrl, { method: 'DELETE' })
      .then(refreshFromServer)
      .then(function () { setSubmitting(false); })
      .catch(function (err) { showError('Could not disable sharing: ' + err.message); setSubmitting(false); });
  };

  window.copyUrl = function () {
    var input = document.getElementById('share-url');
    navigator.clipboard.writeText(input.value);
    var btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = 'Copy'; }, 2000);
  };

  // Cloudflare can take a few seconds after sharing is enabled to publish the
  // Access app at the edge. Until then the hostname does not return the Access
  // login redirect, so revealing the URL immediately makes forwarding look
  // broken. Poll the minds-side readiness probe (which checks for the Access
  // 302) and only reveal the link once the edge is live, or after a short
  // timeout with a "may take a moment" note so the user is never stuck.
  var READINESS_POLL_INTERVAL_MS = 2000;
  var READINESS_MAX_ATTEMPTS = 12;
  // Bumped whenever the shown URL changes or sharing is disabled, so a
  // stale in-flight poll loop stops driving the url-section.
  var pollGeneration = 0;

  function revealShareUrl(showFallbackNote) {
    document.getElementById('url-provisioning').classList.add('hidden');
    document.getElementById('url-ready').classList.remove('hidden');
    if (showFallbackNote) {
      document.getElementById('url-fallback-note').classList.remove('hidden');
    }
  }

  function startReadinessPolling(url) {
    document.getElementById('url-section').classList.remove('hidden');
    document.getElementById('url-provisioning').classList.remove('hidden');
    document.getElementById('url-ready').classList.add('hidden');
    var generation = ++pollGeneration;
    var attempts = 0;
    function poll() {
      if (generation !== pollGeneration) return;
      attempts++;
      var probeUrl = statusUrl + '/readiness?url=' + encodeURIComponent(url);
      fetch(probeUrl)
        .then(function (r) { return r.ok ? r.json() : { ready: false }; })
        .catch(function () { return { ready: false }; })
        .then(function (data) {
          if (generation !== pollGeneration) return;
          if (data && data.ready) {
            revealShareUrl(false);
          } else if (attempts >= READINESS_MAX_ATTEMPTS) {
            revealShareUrl(true);
          } else {
            setTimeout(poll, READINESS_POLL_INTERVAL_MS);
          }
        });
    }
    poll();
  }

  // The status endpoint emits the AuthPolicy shape from the imbue_cloud
  // plugin: ``{"emails": [...], "email_domains": [...], "require_idp": ...}``.
  function emailsFromPolicy(policy) {
    if (!policy || !Array.isArray(policy.emails)) return [];
    return policy.emails.slice();
  }

  // -- Server-state application -------------------------------------------
  //
  // The editor is visible from the first paint, seeded with the config's
  // initialEmails; the status GET reconciles it in place when it lands, and
  // the same GET refreshes the editor after Update/Disable. Nothing ever
  // hides the editor or reloads the page (a reload would blank the sharing
  // modal's overlay iframe).

  // The server renders the disabled-state chrome ("Share ...?" heading, a
  // "Share" action button, no Disable button, no URL section), so this starts
  // false and setChrome only touches the DOM when the state actually flips.
  var isEnabled = false;
  // The URL the visible url-section currently reflects, so a post-save
  // refresh that returns the same URL leaves the section (and any readiness
  // poll already running) alone.
  var shownShareUrl = '';

  function setChrome(enabled) {
    if (enabled === isEnabled) return;
    isEnabled = enabled;
    document.getElementById('action-btn').textContent = enabled ? 'Update' : 'Share';
    setHeading(enabled);
    var disableBtn = document.getElementById('disable-btn');
    if (disableBtn) disableBtn.classList.toggle('hidden', !enabled);
    if (!enabled) {
      document.getElementById('url-section').classList.add('hidden');
      shownShareUrl = '';
      pollGeneration++;
    }
  }

  function applyShareUrl(url) {
    if (!url || url === shownShareUrl) return;
    shownShareUrl = url;
    document.getElementById('share-url').value = url;
    startReadinessPolling(url);
  }

  function applyServerState(data) {
    var serverEmails = emailsFromPolicy(data.policy);
    if (data.enabled) {
      existing = serverEmails.slice();
      // Committed edits fold into the server list; only still-pending
      // drafts survive as +/- rows.
      added = added.filter(function (e) { return existing.indexOf(e) < 0; });
      removed = removed.filter(function (e) { return existing.indexOf(e) >= 0; });
    } else {
      existing = [];
      removed = [];
      // Treat the default policy (owner email) as the editor's initial
      // draft so the user sees their own email pre-populated.
      serverEmails.forEach(function (e) {
        if (added.indexOf(e) < 0) added.push(e);
      });
    }
    proposedEmails.forEach(function (e) {
      if (existing.indexOf(e) < 0 && added.indexOf(e) < 0) {
        added.push(e);
      }
    });
    setChrome(!!data.enabled);
    if (data.enabled) applyShareUrl(data.url);
    renderACL();
  }

  function refreshFromServer() {
    return requestWithErrorCheck(statusUrl)
      .then(function (r) { return r.json(); })
      .then(applyServerState);
  }

  // First paint: the editor chrome is visible immediately, with a template-
  // rendered "Loading access list..." placeholder holding the list area (no
  // optimistic emails -- the list appears once the status GET confirms it,
  // and never unloads after that). Share/Update and Disable stay disabled
  // until that first load so a save can't race the initial state.
  function setActionsEnabled(enabled) {
    var actionBtn = document.getElementById('action-btn');
    var disableBtn = document.getElementById('disable-btn');
    if (actionBtn) actionBtn.disabled = !enabled;
    if (disableBtn) disableBtn.disabled = !enabled;
  }
  setActionsEnabled(false);

  refreshFromServer()
    .then(function () { setActionsEnabled(true); })
    .catch(function (err) {
      var container = document.getElementById('email-list');
      if (container) {
        container.textContent = '';
        var failed = document.createElement('p');
        failed.className = 'type-body text-tertiary';
        failed.textContent = "Couldn't load the access list.";
        container.appendChild(failed);
      }
      showError('Failed to load sharing status: ' + err.message);
    });
})();
