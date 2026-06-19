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
    h.textContent = '';
    h.appendChild(document.createTextNode(isEnabled ? '' : 'Share '));

    var codeEl = document.createElement('code');
    codeEl.className = 'code-pill';
    codeEl.textContent = serviceName;
    h.appendChild(codeEl);

    h.appendChild(document.createTextNode(isEnabled ? ' shared in ' : ' in '));

    var link = document.createElement('a');
    link.href = mngrForwardOrigin + '/goto/' + agentId + '/';
    link.className = 'text-accent hover:underline';
    link.textContent = wsName;
    h.appendChild(link);

    if (accountEmail) {
      h.appendChild(document.createTextNode(' ('));
      var acctLink = document.createElement('a');
      acctLink.href = '/accounts';
      acctLink.className = 'text-accent hover:underline';
      acctLink.textContent = accountEmail;
      h.appendChild(acctLink);
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
  function postWithErrorCheck(url, body) {
    return fetch(url, { method: 'POST', body: body }).then(function (r) {
      if (r.ok) return r;
      return r.text().then(function (text) {
        var detail = text;
        try {
          var parsed = JSON.parse(text);
          if (parsed && typeof parsed.error === 'string') detail = parsed.error;
          else if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
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
    var form = new FormData();
    form.append('emails', JSON.stringify(getFinalEmails()));
    var url = '/sharing/' + agentId + '/' + serviceName + '/enable';
    postWithErrorCheck(url, form)
      .then(function () { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
      .catch(function (err) { showError('Could not save sharing changes: ' + err.message); setSubmitting(false); });
  };

  window.submitDisable = function () {
    clearError();
    setSubmitting(true);
    postWithErrorCheck('/sharing/' + agentId + '/' + serviceName + '/disable', null)
      .then(function () { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
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
    var attempts = 0;
    function poll() {
      attempts++;
      var probeUrl = '/api/sharing-readiness/' + agentId + '/' + serviceName + '?url=' + encodeURIComponent(url);
      fetch(probeUrl)
        .then(function (r) { return r.ok ? r.json() : { ready: false }; })
        .catch(function () { return { ready: false }; })
        .then(function (data) {
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

  fetch('/api/sharing-status/' + agentId + '/' + serviceName)
    .then(function (r) {
      if (!r.ok) {
        return r.text().then(function (text) {
          var detail = text;
          try {
            var parsed = JSON.parse(text);
            if (parsed && typeof parsed.error === 'string') detail = parsed.error;
            else if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
          } catch (_) { /* leave as raw */ }
          throw new Error(detail || ('HTTP ' + r.status));
        });
      }
      return r.json();
    })
    .then(function (data) {
      document.getElementById('loading-state').classList.add('hidden');
      document.getElementById('editor-content').classList.remove('hidden');

      var serverEmails = emailsFromPolicy(data.policy);

      if (data.enabled) {
        existing = serverEmails;
        document.getElementById('action-btn').textContent = 'Update';
        setHeading(true);
        if (data.url) {
          document.getElementById('share-url').value = data.url;
          startReadinessPolling(data.url);
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
      }
      proposedEmails.forEach(function (e) {
        if (existing.indexOf(e) < 0 && added.indexOf(e) < 0) {
          added.push(e);
        }
      });
      renderACL();
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
