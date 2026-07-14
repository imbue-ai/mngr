// Workspace Connections page: pending permission requests (Approve/Deny with
// the full latchkey fragments) + revocation of the workspace's granted
// connectors / file sharing / delegation.
//
// Several request fragments can be on the page at once and they carry fixed
// element ids, so ALL fragment interactivity here is scoped per-card: lookups
// go through the enclosing .connections-request container, and the fragments'
// inline handlers pass `this` so the shared globals can find their card.
(function () {
  var pageAgentId = (document.body && document.body.dataset.agentId) || '';

  function cardFor(el) {
    return el && el.closest ? el.closest('.connections-request') : null;
  }

  // -- Deep-link highlight ---------------------------------------------------
  // ?selected=<id> carries an accent ring (server-rendered class); scroll it
  // into view, and clear the ring on the user's first interaction anywhere.
  (function () {
    var selected = document.querySelector('.connections-request.is-selected');
    if (!selected) return;
    try {
      selected.scrollIntoView({ block: 'start', behavior: 'smooth' });
    } catch (e) {
      selected.scrollIntoView();
    }
    document.addEventListener(
      'pointerdown',
      function () {
        selected.classList.remove('is-selected');
      },
      { once: true },
    );
  })();

  // -- Approve-state plumbing (per card) --------------------------------------

  // The predefined-permission dialog offers a catch-all permission (stored as
  // ``any``, shown as ``all``) alongside the specific ones. While ``all`` is
  // ticked, the specific checkboxes are disabled (they keep their own state)
  // to communicate that the catch-all already covers them. The browser omits
  // disabled inputs from the submission, so only the active side is granted.
  function syncPermissionWildcardExclusivity(card) {
    var form = card.querySelector('#permissions-form');
    if (!form) return;
    var wildcard = form.querySelector('input[name="permissions"][data-wildcard]');
    if (!wildcard) return;
    form.querySelectorAll('input[name="permissions"]').forEach(function (box) {
      if (box !== wildcard) box.disabled = wildcard.checked;
    });
  }

  // Parse the WebDAV mount roots the file-sharing dialog embeds on its path
  // input (a JSON array of absolute paths). Returns [] when absent/malformed.
  function sharePathRoots(input) {
    try {
      var parsed = JSON.parse(input.getAttribute('data-allowed-roots') || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  // Expand a leading ``~`` / ``~/`` to the home directory the dialog embeds,
  // mirroring the server-side ``_expand_home_prefix``. ``~user`` is left
  // unchanged so the within-roots check rejects it, matching the server.
  function expandSharePathHome(value, input) {
    var home = String(input.getAttribute('data-home-dir') || '');
    if (!home) return value;
    if (value === '~' || value.indexOf('~/') === 0) {
      return home + value.slice(1);
    }
    return value;
  }

  // Whether ``value`` is at or beneath one of ``roots``. Case-insensitive and
  // purely lexical, mirroring the server-side check.
  function isSharePathWithinRoots(value, roots) {
    if (!value) return false;
    var lower = value.toLowerCase();
    return roots.some(function (root) {
      var r = String(root).replace(/\/+$/, '').toLowerCase() || '/';
      return lower === r || lower.indexOf(r + '/') === 0;
    });
  }

  function updateApproveStateForCard(card) {
    if (!card) return;
    var form = card.querySelector('#permissions-form');
    if (!form) return;
    var approveBtn = card.querySelector('#permissions-approve-btn');
    if (!approveBtn) return;
    syncPermissionWildcardExclusivity(card);
    var anyChecked = form.querySelector('input[name="permissions"]:checked') !== null;
    // The file-sharing dialog adds an editable path field. Its hidden
    // "permissions" checkbox is always checked, so gate Approve on the path
    // being both non-empty AND inside a shared root, with an instant hint for
    // a non-empty out-of-root path (matching the server-side rejection).
    var pathInput = card.querySelector('#file-sharing-path-input');
    var pathOk = true;
    if (pathInput) {
      var value = expandSharePathHome(pathInput.value.trim(), pathInput);
      var withinRoots = isSharePathWithinRoots(value, sharePathRoots(pathInput));
      pathOk = value.length > 0 && withinRoots;
      var hint = card.querySelector('#file-sharing-path-hint');
      if (hint) hint.classList.toggle('hidden', !(value.length > 0 && !withinRoots));
    }
    approveBtn.disabled = !(anyChecked && pathOk);
  }

  // Inline-handler entry point (the file-sharing path input passes `this`).
  window.updateApproveState = function (el) {
    updateApproveStateForCard(cardFor(el));
  };

  // The predefined-permission "Adjust" toggle: reveal the checkbox editor.
  window.showPermissionEditor = function (el) {
    var card = cardFor(el);
    if (!card) return;
    var simple = card.querySelector('#permissions-simple-view');
    var editor = card.querySelector('#permissions-editor-view');
    if (simple) simple.classList.add('hidden');
    if (editor) editor.classList.remove('hidden');
  };

  // The file-sharing dialog ships its "Choose file/folder…" buttons hidden;
  // reveal them only when the Electron native-picker bridge is available.
  // This page runs in the content view, which has no window.minds bridge, so
  // in practice they stay hidden there and the user pastes a path instead.
  function wireSharePathControls(card) {
    if (!(window.minds && window.minds.showFilePicker)) return;
    ['#file-sharing-browse-file-btn', '#file-sharing-browse-folder-btn'].forEach(function (sel) {
      var btn = card.querySelector(sel);
      if (btn) btn.classList.remove('hidden');
    });
  }

  window.browseForSharePath = async function (el, mode) {
    var card = cardFor(el);
    if (!card) return;
    var input = card.querySelector('#file-sharing-path-input');
    if (!input || !(window.minds && window.minds.showFilePicker)) return;
    try {
      var selected = await window.minds.showFilePicker({
        defaultPath: input.value.trim(),
        mode: mode === 'directory' ? 'directory' : 'file',
      });
      if (typeof selected === 'string' && selected.length > 0) {
        input.value = selected;
        updateApproveStateForCard(card);
      }
    } catch (e) {
      /* user cancelled or the bridge errored -- keep the current path */
    }
  };

  // -- Approve / Deny submission ---------------------------------------------

  // True while any approval POST is in flight; the SSE-driven reload below
  // holds off so an in-progress flow (browser sign-in, manual credentials)
  // isn't clobbered by a page refresh.
  var approveInFlight = false;

  function scrollCardNoticeIntoView(el) {
    if (!el) return;
    try {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    } catch (e) {
      /* older browsers */
    }
  }

  function setApproveBusy(card, isBusy) {
    var approveBtn = card.querySelector('#permissions-approve-btn');
    var denyBtn = card.querySelector('#permissions-deny-btn');
    var spinner = card.querySelector('#permissions-approve-spinner');
    var label = card.querySelector('#permissions-approve-label');
    if (isBusy) {
      if (approveBtn) approveBtn.disabled = true;
      if (denyBtn) denyBtn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');
      if (label) label.textContent = 'Approving…';
    } else {
      if (denyBtn) denyBtn.disabled = false;
      if (spinner) spinner.classList.add('hidden');
      if (label) label.textContent = 'Approve';
      updateApproveStateForCard(card);
    }
  }

  async function submitGrant(card, form) {
    var errorBox = card.querySelector('#permissions-error');
    var errorMsg = card.querySelector('#permissions-error-message');
    var manualBox = card.querySelector('#permissions-manual-credentials');
    var progress = card.querySelector('#permissions-progress');
    approveInFlight = true;
    setApproveBusy(card, true);
    if (errorBox) errorBox.classList.add('hidden');
    if (manualBox) manualBox.classList.add('hidden');
    if (progress) {
      progress.classList.remove('hidden');
      scrollCardNoticeIntoView(progress);
    }

    var formData = new FormData(form);
    try {
      var response = await fetch(form.action, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
      });
      if (!response.ok) {
        var text = await response.text();
        throw new Error(text || 'HTTP ' + response.status);
      }
      var data = await response.json();
      if (data.outcome === 'GRANTED' || data.outcome === 'DENIED') {
        // Re-render: the request leaves "Waiting on you" and any fresh grant
        // appears in the Connected section.
        approveInFlight = false;
        window.location.reload();
        return;
      }
      if (progress) progress.classList.add('hidden');
      if (data.outcome === 'NEEDS_MANUAL_CREDENTIALS') {
        var cmdEl = card.querySelector('#permissions-manual-credentials-command');
        var msgEl = card.querySelector('#permissions-manual-credentials-message');
        if (cmdEl) cmdEl.textContent = data.set_credentials_example || '';
        if (msgEl) msgEl.textContent = data.message || '';
        if (manualBox) {
          manualBox.classList.remove('hidden');
          scrollCardNoticeIntoView(manualBox);
        }
        approveInFlight = false;
        setApproveBusy(card, false);
        return;
      }
      // FAILED (or anything else): the request stays pending server-side;
      // show the reason and re-enable Approve so the user can retry.
      if (errorMsg) errorMsg.textContent = data.message || 'Approval failed; please try again.';
      if (errorBox) {
        errorBox.classList.remove('hidden');
        scrollCardNoticeIntoView(errorBox);
      }
      approveInFlight = false;
      setApproveBusy(card, false);
    } catch (err) {
      if (progress) progress.classList.add('hidden');
      if (errorMsg) errorMsg.textContent = err && err.message ? err.message : String(err);
      if (errorBox) {
        errorBox.classList.remove('hidden');
        scrollCardNoticeIntoView(errorBox);
      }
      approveInFlight = false;
      setApproveBusy(card, false);
    }
  }

  // Deny is fire-and-forget (the user shouldn't wait for the mngr message
  // round trip); the card fades and freezes until the SSE-driven reload
  // drops it from the pending set.
  window.submitPermissionDeny = function (el) {
    var card = cardFor(el);
    if (!card) return;
    var form = card.querySelector('#permissions-form');
    if (!form) return;
    card.classList.add('is-denying');
    var approveBtn = card.querySelector('#permissions-approve-btn');
    if (approveBtn) approveBtn.disabled = true;
    var denyBtn = card.querySelector('#permissions-deny-btn');
    if (denyBtn) denyBtn.disabled = true;
    var denyUrl = form.action.replace(/\/grant\b/, '/deny');
    fetch(denyUrl, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
    }).catch(function () {});
  };

  // -- Event delegation over the request cards --------------------------------

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.id !== 'permissions-form') return;
    e.preventDefault();
    var card = cardFor(form);
    if (card) submitGrant(card, form);
  });

  document.addEventListener('change', function (e) {
    if (e.target && e.target.name === 'permissions') {
      updateApproveStateForCard(cardFor(e.target));
    }
  });

  // Initial state per card: Approve availability + native-picker reveal.
  document.querySelectorAll('.connections-request').forEach(function (card) {
    updateApproveStateForCard(card);
    wireSharePathControls(card);
  });

  // -- SSE-driven refresh ------------------------------------------------------
  //
  // Reload when this workspace's pending-request set changes (a new request
  // arrived, or one was resolved -- possibly in another window), so the page
  // is never stale. Held while an approval is in flight so an in-progress
  // flow isn't clobbered.
  var renderedIds = Array.prototype.map
    .call(document.querySelectorAll('.connections-request'), function (card) {
      return card.getAttribute('data-request-id');
    })
    .sort();

  var evtSource = null;
  function connectSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/_chrome/events');
    evtSource.onmessage = function (event) {
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return;
      }
      if (data.type !== 'requests') return;
      var entries = Array.isArray(data.requests) ? data.requests : [];
      var liveIds = entries
        .filter(function (entry) {
          return entry && entry.workspace_agent_id === pageAgentId;
        })
        .map(function (entry) {
          return String(entry.id);
        })
        .sort();
      if (liveIds.join(',') === renderedIds.join(',')) return;
      if (approveInFlight) return;
      evtSource.close();
      window.location.reload();
    };
    evtSource.onerror = function () {
      if (!evtSource) return;
      evtSource.close();
      evtSource = null;
      setTimeout(connectSSE, 5000);
    };
  }
  connectSSE();

  // -- Revocation --------------------------------------------------------------

  var revokeDialog = document.getElementById('revoke-dialog');
  var revokeTitle = document.getElementById('revoke-dialog-title');
  var revokeBody = document.getElementById('revoke-dialog-body');
  var revokeCancelBtn = document.getElementById('revoke-cancel-btn');
  var revokeConfirmBtn = document.getElementById('revoke-confirm-btn');
  var revokeErrorEl = document.getElementById('revoke-error');
  var pendingRevoke = null;

  function openRevokeDialog(title, body, requestSpec) {
    pendingRevoke = requestSpec;
    revokeTitle.textContent = title;
    revokeBody.textContent = body;
    if (revokeErrorEl) revokeErrorEl.classList.add('hidden');
    revokeConfirmBtn.disabled = false;
    revokeDialog.classList.remove('hidden');
  }
  function closeRevokeDialog() {
    pendingRevoke = null;
    revokeDialog.classList.add('hidden');
  }
  if (revokeDialog) {
    revokeCancelBtn.addEventListener('click', closeRevokeDialog);
    revokeDialog.addEventListener('click', function (e) {
      if (e.target === revokeDialog) closeRevokeDialog();
    });
    revokeConfirmBtn.addEventListener('click', function () {
      if (!pendingRevoke) return;
      revokeConfirmBtn.disabled = true;
      fetch(pendingRevoke.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(pendingRevoke.body),
      })
        .then(function (resp) {
          if (resp.ok) {
            window.location.reload();
            return;
          }
          revokeConfirmBtn.disabled = false;
          if (revokeErrorEl) {
            revokeErrorEl.textContent = 'Could not revoke (HTTP ' + resp.status + ')';
            revokeErrorEl.classList.remove('hidden');
          }
        })
        .catch(function () {
          revokeConfirmBtn.disabled = false;
          if (revokeErrorEl) {
            revokeErrorEl.textContent = 'Could not revoke (network error)';
            revokeErrorEl.classList.remove('hidden');
          }
        });
    });
  }

  document.querySelectorAll('.revoke-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var serviceSection = btn.closest('[data-service-name]');
      var card = btn.closest('[data-workspace-agent-id]');
      var serviceLabel = serviceSection.getAttribute('data-service-label');
      var workspaceName = card.getAttribute('data-workspace-name');
      openRevokeDialog(
        'Revoke ' + serviceLabel + ' access?',
        'This removes ' + workspaceName + "'s " + serviceLabel + ' permissions. The agent can request them again later.',
        {
          url: '/settings/permissions/revoke',
          body: {
            workspace_agent_id: card.getAttribute('data-workspace-agent-id'),
            service_name: serviceSection.getAttribute('data-service-name'),
          },
        }
      );
    });
  });

  document.querySelectorAll('.revoke-fs-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var card = btn.closest('[data-workspace-agent-id]');
      var workspaceName = card.getAttribute('data-workspace-name');
      openRevokeDialog(
        'Revoke file sharing?',
        'This removes ' + workspaceName + "'s shared file access. The agent can request it again later.",
        {
          url: '/settings/permissions/file-sharing/revoke',
          body: { workspace_agent_id: card.getAttribute('data-workspace-agent-id') },
        }
      );
    });
  });

  document.querySelectorAll('.revoke-verb-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var group = btn.closest('[data-workspace-agent-id]');
      var row = btn.closest('[data-verb-permission]');
      var workspaceName = group.getAttribute('data-workspace-name');
      var verbLabel = row.getAttribute('data-verb-label');
      openRevokeDialog(
        'Revoke ' + verbLabel + ' access?',
        'This removes ' + workspaceName + "'s " + verbLabel + ' access to other workspaces. The agent can request it again later.',
        {
          url: '/settings/permissions/workspace/revoke',
          body: {
            workspace_agent_id: group.getAttribute('data-workspace-agent-id'),
            verb: row.getAttribute('data-verb-permission'),
          },
        }
      );
    });
  });
})();
