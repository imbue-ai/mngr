// Requests inbox modal: list + detail behavior.
//
// Lives inside a transparent Electron modal overlay (or, in browser
// mode, inline in the chrome). Drives row selection, detail-pane
// fetching, grant/deny form submission, auto-advance on resolve, the
// auto-open checkbox, and click-out/Escape close.
(function () {
  var isElectron = !!window.minds;

  var backdrop = document.getElementById('requests-inbox-backdrop');
  if (!backdrop) return;

  var listEl = document.getElementById('requests-inbox-list');
  var detailEl = document.getElementById('requests-inbox-detail');
  var checkbox = document.getElementById('requests-inbox-auto-open-checkbox');

  var currentDetailEventId = backdrop.dataset.initialDetailEventId || '';
  var initialEventId = backdrop.dataset.initialEventId || '';

  // -- Close -----------------------------------------------------------------
  window.closeRequestsInbox = function () {
    if (isElectron && window.minds.closeModal) {
      window.minds.closeModal();
      return;
    }
    // Browser mode: when loaded inside the chrome page's host iframe,
    // tell the parent to hide us; the parent owns the show/hide CSS so
    // the workspace behind stays visible. When opened directly in a
    // standalone tab there is no parent to message; fall back to navigating
    // home.
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage({ type: 'minds:close-requests-inbox' }, window.location.origin);
        return;
      } catch (e) {}
    }
    window.location.href = '/';
  };

  window.onRequestsBackdropClick = function (event) {
    if (event.target && event.target.id === 'requests-inbox-backdrop') {
      window.closeRequestsInbox();
    }
  };

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.closeRequestsInbox();
  });

  // -- Row selection ---------------------------------------------------------
  function highlightSelected(eventId) {
    var rows = listEl ? listEl.querySelectorAll('[data-event-id]') : [];
    rows.forEach(function (r) {
      if (r.getAttribute('data-event-id') === eventId) {
        r.classList.add('bg-white', 'border-zinc-300');
        r.classList.remove('hover:bg-zinc-100');
      } else {
        r.classList.remove('bg-white', 'border-zinc-300');
        r.classList.add('hover:bg-zinc-100');
      }
    });
  }

  function loadDetail(eventId) {
    if (!eventId) {
      currentDetailEventId = '';
      if (detailEl) detailEl.innerHTML = '<div class="text-zinc-400 text-sm flex items-center justify-center h-full">Select a request from the list.</div>';
      return;
    }
    currentDetailEventId = eventId;
    highlightSelected(eventId);
    fetch('/_chrome/requests-inbox/detail/' + encodeURIComponent(eventId), {
      credentials: 'same-origin',
    }).then(function (resp) {
      return resp.text().then(function (text) { return { ok: resp.ok, text: text }; });
    }).then(function (result) {
      // If the user clicked a different row before this fetch finished,
      // the latest selection wins; drop this response.
      if (currentDetailEventId !== eventId) return;
      if (detailEl) {
        detailEl.innerHTML = result.text;
        if (result.ok) wirePermissionForm();
      }
    }).catch(function () {
      if (currentDetailEventId !== eventId) return;
      if (detailEl) detailEl.innerHTML = '<div class="text-rose-600 text-sm">Failed to load request.</div>';
    });
  }

  if (listEl) {
    listEl.addEventListener('click', function (e) {
      var row = e.target.closest('[data-event-id]');
      if (!row) return;
      // Unknown-scope fragment's Deny button.
      var denyBtn = e.target.closest('[data-deny-request-id]');
      if (denyBtn) {
        submitDeny(denyBtn.getAttribute('data-deny-request-id'));
        return;
      }
      loadDetail(row.getAttribute('data-event-id'));
    });
  }

  // -- Auto-open checkbox ----------------------------------------------------
  if (checkbox) {
    checkbox.addEventListener('change', function () {
      fetch('/_chrome/requests-auto-open', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: checkbox.checked }),
      }).catch(function () {});
    });
  }

  // -- Permission form wiring (per-detail) -----------------------------------
  //
  // The detail fragment carries a <form id="permissions-form"> with name="permissions"
  // checkboxes (mirroring the existing standalone PermissionsDialog). Each time we
  // swap detail-pane content we wire fresh submit/deny handlers on the new form.
  function wirePermissionForm() {
    var form = document.getElementById('permissions-form');
    if (!form) {
      // Unknown-scope fragment: a single Deny button with data-deny-request-id.
      var unknownDeny = detailEl ? detailEl.querySelector('[data-deny-request-id]') : null;
      if (unknownDeny) {
        unknownDeny.addEventListener('click', function () {
          submitDeny(unknownDeny.getAttribute('data-deny-request-id'));
        });
      }
      return;
    }
    var approveBtn = document.getElementById('permissions-approve-btn');
    var errorBox = document.getElementById('permissions-error');
    var errorMsg = document.getElementById('permissions-error-message');

    function updateApproveState() {
      var anyChecked = form.querySelector('input[name="permissions"]:checked') !== null;
      if (approveBtn) approveBtn.disabled = !anyChecked;
    }
    window.updateApproveState = updateApproveState;
    form.querySelectorAll('input[name="permissions"]').forEach(function (cb) {
      cb.addEventListener('change', updateApproveState);
    });
    updateApproveState();

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (approveBtn) approveBtn.disabled = true;
      if (errorBox) errorBox.classList.add('hidden');
      var manualBox = document.getElementById('permissions-manual-credentials');
      if (manualBox) manualBox.classList.add('hidden');
      var progress = document.getElementById('permissions-progress');
      if (progress) progress.classList.remove('hidden');

      var formData = new FormData(form);
      fetch(form.action, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
      }).then(function (response) {
        if (!response.ok) {
          return response.text().then(function (t) { throw new Error(t || ('HTTP ' + response.status)); });
        }
        return response.json();
      }).then(function (data) {
        if (data.outcome === 'GRANTED') {
          // The SSE-driven list refresh will remove this row; advance the
          // detail pane to whatever's now at the top, or close the modal
          // if the inbox just emptied. handleListUpdated below covers both.
          return;
        }
        if (progress) progress.classList.add('hidden');
        if (data.outcome === 'NEEDS_MANUAL_CREDENTIALS') {
          var cmdEl = document.getElementById('permissions-manual-credentials-command');
          var msgEl = document.getElementById('permissions-manual-credentials-message');
          if (cmdEl) cmdEl.textContent = data.set_credentials_example || '';
          if (msgEl) msgEl.textContent = data.message || '';
          if (manualBox) manualBox.classList.remove('hidden');
          updateApproveState();
          return;
        }
        if (errorMsg) errorMsg.textContent = data.message || 'Authorization failed.';
        if (errorBox) errorBox.classList.remove('hidden');
        if (approveBtn) approveBtn.disabled = false;
      }).catch(function (err) {
        if (progress) progress.classList.add('hidden');
        if (errorMsg) errorMsg.textContent = err && err.message ? err.message : String(err);
        if (errorBox) errorBox.classList.remove('hidden');
        if (approveBtn) approveBtn.disabled = false;
      });
    });

    window.submitPermissionDeny = function () {
      var denyUrl = form.action.replace(/\/grant\b/, '/deny');
      submitDeny(null, denyUrl);
    };
  }

  function submitDeny(requestId, denyUrlOverride) {
    var url = denyUrlOverride;
    if (!url && requestId) url = '/requests/' + encodeURIComponent(requestId) + '/deny';
    if (!url) return;
    // Fire-and-forget: don't make the user wait for the server to notify
    // the agent. keepalive: true lets the request outlive the navigation
    // so the response event still gets written.
    fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
    }).catch(function () {});
    // The list will refresh from SSE; meanwhile show a "Select a request"
    // placeholder so the user sees their action took effect.
    loadDetail('');
  }

  // -- Auto-advance / list refresh ------------------------------------------
  //
  // Whenever the chrome SSE pushes a new requests payload, the host
  // (Electron main process or chrome.js) calls back into this page to
  // refresh the list fragment. We then either keep the current detail
  // (if still pending), advance to the next pending request, or close
  // the modal (inbox empty).
  function refreshListFromServer() {
    fetch('/_chrome/requests-inbox/list', { credentials: 'same-origin' })
      .then(function (resp) { return resp.text(); })
      .then(function (text) {
        if (!listEl) return;
        listEl.innerHTML = text;
        handleListUpdated();
      })
      .catch(function () {});
  }

  function handleListUpdated() {
    var rows = listEl ? listEl.querySelectorAll('[data-event-id]') : [];
    if (!rows.length) {
      window.closeRequestsInbox();
      return;
    }
    var stillPresent = false;
    rows.forEach(function (r) {
      if (r.getAttribute('data-event-id') === currentDetailEventId) stillPresent = true;
    });
    if (currentDetailEventId && stillPresent) {
      highlightSelected(currentDetailEventId);
      return;
    }
    // Detail pane is on a request that just got resolved (or empty).
    // Auto-advance to the top of the list.
    var nextId = rows[0].getAttribute('data-event-id');
    loadDetail(nextId);
  }

  function handleChromeEvent(data) {
    if (data && data.type === 'requests') {
      refreshListFromServer();
    }
  }
  if (isElectron && window.minds && window.minds.onChromeEvent) {
    window.minds.onChromeEvent(handleChromeEvent);
  } else {
    var evtSource = null;
    function connectSSE() {
      if (evtSource) evtSource.close();
      evtSource = new EventSource('/_chrome/events');
      evtSource.onmessage = function (event) {
        try { handleChromeEvent(JSON.parse(event.data)); } catch (e) {}
      };
      evtSource.onerror = function () {
        evtSource.close();
        evtSource = null;
        setTimeout(connectSSE, 5000);
      };
    }
    connectSSE();
  }

  // -- Initial state --------------------------------------------------------
  // On load, wire the form (if the server pre-rendered a detail) and
  // auto-select the initial event when no detail was pre-rendered.
  if (currentDetailEventId) {
    wirePermissionForm();
    highlightSelected(currentDetailEventId);
  } else if (initialEventId) {
    loadDetail(initialEventId);
  }
})();
