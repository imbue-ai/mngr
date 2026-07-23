// Sharing editor: rebuilds the ACL + heading via DOM methods (NOT innerHTML)
// so a crafted email cannot inject script. The page config is passed from
// Jinja as a JSON data island, not as template-interpolated JS.
//
// Runs on two surfaces sharing the same markup (templates/SharingEditor.jinja):
// the full /sharing page (browser mode) and the centered sharing modal hosted
// in the overlay surface (Electron). Behavior matches the full page exactly,
// with one difference: after Update/Disable the editor re-fetches the sharing
// state in place instead of navigating to the page URL -- a navigation (or
// reload) would blank the modal's overlay iframe, so the editor stays visible
// (grayed out via setSubmitting) until the fresh state is applied.
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

  function renderACL() {
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
    var actionBtns = document.getElementById('action-buttons');
    actionBtns.classList.toggle('hidden', submitting);
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

  window.submitUpdate = function () {
    clearError();
    setSubmitting(true);
    var url = '/api/v1/workspaces/' + agentId + '/sharing/' + serviceName;
    requestWithErrorCheck(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: getFinalEmails() }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // The enable response carries the final state (including the share
        // URL), so render it in place -- no page reload, no follow-up
        // status fetch.
        existing = getFinalEmails();
        added = [];
        removed = [];
        document.getElementById('action-btn').textContent = 'Update';
        setHeading(true);
        var disableBtn = document.getElementById('disable-btn');
        if (disableBtn) disableBtn.classList.remove('hidden');
        renderACL();
        setSubmitting(false);
        if (data && data.url) {
          document.getElementById('share-url').value = data.url;
          startReadinessPolling(data.url, true);
        }
      })
      .catch(function (err) { showError('Could not save sharing changes: ' + err.message); setSubmitting(false); });
  };

  window.submitDisable = function () {
    clearError();
    setSubmitting(true);
    requestWithErrorCheck('/api/v1/workspaces/' + agentId + '/sharing/' + serviceName, { method: 'DELETE' })
      .then(refreshAfterSave)
      .catch(function (err) { showError('Could not disable sharing: ' + err.message); setSubmitting(false); });
  };

  window.copyUrl = function () {
    var input = document.getElementById('share-url');
    navigator.clipboard.writeText(input.value);
    var btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = 'Copy'; }, 2000);
  };

  // Cloudflare needs time to publish a (re)created hostname at the edge --
  // empirically 1-2 minutes is normal, and a hostname that was just deleted
  // (a disable -> enable cycle) can take longer than a brand-new one. While
  // waiting, a prominent warning box replaces the URL entirely (so nobody
  // copies a link that does not work yet); "Show anyway" reveals it early
  // for users who want to send it ahead. Poll fast at first, then back off;
  // at the deadline reveal the URL with a still-not-live warning instead of
  // quietly pretending success.
  var READINESS_FAST_INTERVAL_MS = 2000;
  var READINESS_SLOW_INTERVAL_MS = 5000;
  var READINESS_FAST_PHASE_MS = 30 * 1000;
  var READINESS_DEADLINE_MS = 5 * 60 * 1000;

  var isUrlRevealedEarly = false;

  window.showUrlAnyway = function () {
    isUrlRevealedEarly = true;
    document.getElementById('url-ready').classList.remove('hidden');
    var btn = document.getElementById('show-url-anyway-btn');
    if (btn) btn.classList.add('hidden');
  };

  function markShareUrlLive() {
    document.getElementById('url-provisioning').classList.add('hidden');
    document.getElementById('url-fallback-note').classList.add('hidden');
    document.getElementById('url-ready').classList.remove('hidden');
  }

  function markShareUrlStillNotLive() {
    document.getElementById('url-provisioning').classList.add('hidden');
    document.getElementById('url-fallback-note').classList.remove('hidden');
    document.getElementById('url-ready').classList.remove('hidden');
  }

  // ``isFreshlyEnabled`` distinguishes the two callers: right after an
  // enable, the hostname was just (re)created, so the warning box shows
  // immediately and the URL stays hidden behind it until a probe confirms
  // the link. On plain page load of an existing share the link is almost
  // always long-live, so the URL shows immediately and NOTHING else is
  // rendered until the first probe answers -- only if that probe reports
  // not-ready does the warning appear (no flash for healthy links).
  function startReadinessPolling(url, isFreshlyEnabled) {
    document.getElementById('url-section').classList.remove('hidden');
    document.getElementById('url-provisioning').classList.toggle('hidden', !isFreshlyEnabled);
    document.getElementById('url-fallback-note').classList.add('hidden');
    var isUrlHidden = isFreshlyEnabled && !isUrlRevealedEarly;
    var showAnywayBtn = document.getElementById('show-url-anyway-btn');
    if (showAnywayBtn) showAnywayBtn.classList.toggle('hidden', !isUrlHidden);
    document.getElementById('url-ready').classList.toggle('hidden', isUrlHidden);
    var startedAt = Date.now();
    function poll() {
      var probeUrl = '/api/v1/workspaces/' + agentId + '/sharing/' + serviceName + '/readiness?url=' + encodeURIComponent(url);
      fetch(probeUrl)
        .then(function (r) { return r.ok ? r.json() : { ready: false }; })
        .catch(function () { return { ready: false }; })
        .then(function (data) {
          var elapsedMs = Date.now() - startedAt;
          if (data && data.ready) {
            markShareUrlLive();
          } else if (elapsedMs >= READINESS_DEADLINE_MS) {
            markShareUrlStillNotLive();
          } else {
            document.getElementById('url-provisioning').classList.remove('hidden');
            var interval = elapsedMs < READINESS_FAST_PHASE_MS ? READINESS_FAST_INTERVAL_MS : READINESS_SLOW_INTERVAL_MS;
            setTimeout(poll, interval);
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

  function fetchStatus() {
    return fetch('/api/v1/workspaces/' + agentId + '/sharing/' + serviceName)
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (text) {
            var detail = text;
            try {
              detail = window.normalizeApiError(JSON.parse(text)).message;
            } catch (_) { /* leave as raw */ }
            throw new Error(detail || ('HTTP ' + r.status));
          });
        }
        return r.json();
      });
  }

  // Apply a fresh status payload to the editor -- exactly what the full page
  // computes on load. ``isInitial`` folds the URL-proposed emails in only
  // once (a page reload re-reads them from the URL; the in-place refresh
  // must not re-add drafts the user just committed or discarded).
  function applyLoadedState(data, isInitial) {
    var serverEmails = emailsFromPolicy(data.policy);

    if (data.enabled) {
      existing = serverEmails;
      document.getElementById('action-btn').textContent = 'Update';
      setHeading(true);
      if (data.url) {
        document.getElementById('share-url').value = data.url;
        startReadinessPolling(data.url, false);
      }
      var disableBtn = document.getElementById('disable-btn');
      if (disableBtn) disableBtn.classList.remove('hidden');
    } else {
      // Treat the default policy (owner email) as the editor's
      // initial draft so the user sees their own email pre-populated.
      serverEmails.forEach(function (e) {
        if (added.indexOf(e) < 0) added.push(e);
      });
      document.getElementById('action-btn').textContent = 'Share';
      setHeading(false);
      // A page reload landed on the template's disabled-state chrome; the
      // in-place refresh resets it explicitly.
      var disableBtnOff = document.getElementById('disable-btn');
      if (disableBtnOff) disableBtnOff.classList.add('hidden');
      document.getElementById('url-section').classList.add('hidden');
    }
    if (isInitial) {
      proposedEmails.forEach(function (e) {
        if (existing.indexOf(e) < 0 && added.indexOf(e) < 0) {
          added.push(e);
        }
      });
    }
    renderACL();
  }

  // After a successful Update/Disable, re-fetch and apply the fresh state in
  // place of the full page's navigation-to-self. The editor stays visible
  // and grayed out (setSubmitting) until the state lands.
  function refreshAfterSave() {
    existing = [];
    added = [];
    removed = [];
    return fetchStatus()
      .then(function (data) {
        applyLoadedState(data, false);
        setSubmitting(false);
      })
      .catch(function (err) {
        setSubmitting(false);
        showError('Saved, but refreshing the editor failed: ' + err.message);
      });
  }

  fetchStatus()
    .then(function (data) {
      document.getElementById('loading-state').classList.add('hidden');
      document.getElementById('editor-content').classList.remove('hidden');
      applyLoadedState(data, true);
    })
    .catch(function (err) {
      var state = document.getElementById('loading-state');
      state.textContent = 'Failed to load sharing status: ' + err.message;
      state.className = 'text-important py-4';
      document.getElementById('editor-content').classList.remove('hidden');
      added = proposedEmails.slice();
      renderACL();
    });
})();
