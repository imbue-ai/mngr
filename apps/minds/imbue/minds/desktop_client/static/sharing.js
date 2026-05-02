// Sharing editor: rebuilds the ACL + heading via DOM methods (NOT innerHTML)
// so a crafted email from a sharing request cannot inject script. The page
// config is passed from Jinja as a JSON data island, not as template-interpolated JS.
(function () {
  var configEl = document.getElementById('sharing-config');
  if (!configEl) return;
  var config = JSON.parse(configEl.textContent);
  var agentId = config.agentId;
  var serviceName = config.serviceName;
  var wsName = config.wsName;
  var accountEmail = config.accountEmail;
  var isRequest = config.isRequest;
  var requestId = config.requestId;
  var proposedEmails = config.initialEmails || [];

  function setHeading(isEnabled) {
    var h = document.getElementById('page-heading');
    if (!h) return;
    h.textContent = '';
    h.appendChild(document.createTextNode(isEnabled ? '' : 'Share '));

    var codeEl = document.createElement('code');
    codeEl.className = 'bg-zinc-100 rounded px-1.5 py-0.5 font-mono text-[0.95em]';
    codeEl.textContent = serviceName;
    h.appendChild(codeEl);

    h.appendChild(document.createTextNode(isEnabled ? ' shared in ' : ' in '));

    var link = document.createElement('a');
    link.href = '/goto/' + agentId + '/';
    link.className = 'text-blue-600 hover:underline';
    link.textContent = wsName;
    h.appendChild(link);

    if (accountEmail) {
      h.appendChild(document.createTextNode(' ('));
      var acctLink = document.createElement('a');
      acctLink.href = '/accounts';
      acctLink.className = 'text-blue-600 hover:underline';
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
      existing: 'bg-white border-zinc-200',
      added:    'bg-emerald-50 border-emerald-200',
      removed:  'bg-red-50 border-red-200 line-through',
    }[variant];
    var row = document.createElement('div');
    row.className = base + rowCls;

    var left = document.createElement('span');
    if (variant === 'added' || variant === 'removed') {
      var prefix = document.createElement('span');
      prefix.className = 'font-semibold mr-1.5 ' + (variant === 'added' ? 'text-emerald-600' : 'text-red-600');
      prefix.textContent = variant === 'added' ? '+' : '−';
      left.appendChild(prefix);
    }
    var emailEl = document.createElement('span');
    emailEl.className = 'text-sm ' + (variant === 'removed' ? 'text-zinc-400' : 'text-zinc-800');
    emailEl.textContent = email;
    left.appendChild(emailEl);
    row.appendChild(left);

    var btn = document.createElement('button');
    btn.className = 'bg-transparent border-none cursor-pointer text-zinc-400 text-lg leading-none px-1 hover:text-zinc-600';
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
      empty.className = 'text-sm text-zinc-400';
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

  window.submitUpdate = function () {
    setSubmitting(true);
    var form = new FormData();
    form.append('emails', JSON.stringify(getFinalEmails()));
    // Request-approval and direct-edit submissions go to different
    // endpoints: the request flow needs a GRANTED response event
    // appended (handled by /requests/{id}/grant -> SharingRequestHandler),
    // while direct edits just change the Cloudflare config. Both end up
    // calling the same enable_sharing_via_cloudflare helper server-side.
    var url = isRequest
      ? '/requests/' + requestId + '/grant'
      : '/sharing/' + agentId + '/' + serviceName + '/enable';
    fetch(url, { method: 'POST', body: form })
      .then(function () { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
      .catch(function (err) { alert('Failed: ' + err.message); setSubmitting(false); });
  };

  window.submitDisable = function () {
    setSubmitting(true);
    fetch('/sharing/' + agentId + '/' + serviceName + '/disable', { method: 'POST' })
      .then(function () { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
      .catch(function (err) { alert('Failed: ' + err.message); setSubmitting(false); });
  };

  window.submitDeny = function () {
    setSubmitting(true);
    fetch('/requests/' + requestId + '/deny', { method: 'POST' })
      .then(function () { window.location.href = '/'; })
      .catch(function (err) { alert('Failed: ' + err.message); setSubmitting(false); });
  };

  window.copyUrl = function () {
    var input = document.getElementById('share-url');
    navigator.clipboard.writeText(input.value);
    var btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = 'Copy'; }, 2000);
  };

  fetch('/api/sharing-status/' + agentId + '/' + serviceName)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      document.getElementById('loading-state').classList.add('hidden');
      document.getElementById('editor-content').classList.remove('hidden');

      var serverEmails = [];
      if (data.auth_rules) {
        data.auth_rules.forEach(function (rule) {
          (rule.include || []).forEach(function (inc) {
            if (inc.email && inc.email.email && serverEmails.indexOf(inc.email.email) < 0) {
              serverEmails.push(inc.email.email);
            }
          });
        });
      }

      if (data.enabled) {
        existing = serverEmails;
        document.getElementById('action-btn').textContent = 'Update';
        setHeading(true);
        if (data.url) {
          document.getElementById('url-section').classList.remove('hidden');
          document.getElementById('share-url').value = data.url;
        }
        var disableBtn = document.getElementById('disable-btn');
        if (disableBtn) disableBtn.classList.remove('hidden');
      } else {
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
      state.className = 'text-red-600 py-4';
      document.getElementById('editor-content').classList.remove('hidden');
      added = proposedEmails.slice();
      renderACL();
    });
})();
