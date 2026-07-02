// Inbox (requests) modal logic, single-sourced for both contexts (the auth.js
// pattern):
//   * Electron overlay: registered in window.MINDS_OVERLAY_MODALS so overlay.js
//     injects the ?fragment=1 markup and calls init(container). The host owns the
//     backdrop dismiss, main owns Escape, and live list refresh comes from the
//     host's cached SSE state (window.MINDS_OVERLAY_HOST).
//   * Standalone browser page (/inbox in the content frame): this file auto-runs
//     init(document); with no host it wires its own backdrop dismiss + Escape and
//     falls back to navigating home for close. There is no MINDS_OVERLAY_HOST in
//     the browser, so there is no live SSE refresh -- matching the original inbox
//     page, which only subscribed when window.minds was present.
//
// The server-rendered detail fragments (GET /inbox/detail/<id>) call a set of
// handlers via inline onclick/onchange/oninput, so init assigns them on window in
// both contexts (and clears them on the Electron teardown).
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  var GLOBAL_HANDLER_NAMES = [
    'closeInbox',
    'onAutoOpenToggle',
    'showPermissionEditor',
    'updateApproveState',
    'browseForSharePath',
    'submitPermissionDeny',
  ];

  function initInbox(root) {
    var isElectron = !!(window.minds && window.minds.closeModal);
    var host = window.MINDS_OVERLAY_HOST || {};
    var teardownFns = [];

    function find(selector) {
      return root.querySelector(selector);
    }

    var inboxBody = find('#inbox-body');
    var inboxList = find('#inbox-list');
    var inboxDetail = find('#inbox-detail');

    function closeInbox() {
      if (isElectron) window.minds.closeModal();
      else window.location.href = '/';
    }

    function onAutoOpenToggle(event) {
      var enabled = !!(event.target && event.target.checked);
      fetch('/_chrome/requests-auto-open', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: enabled }),
        keepalive: true,
      }).catch(function () {});
    }

    function showPermissionEditor() {
      var simple = find('#permissions-simple-view');
      var editor = find('#permissions-editor-view');
      if (simple) simple.classList.add('hidden');
      if (editor) editor.classList.remove('hidden');
    }

    function sharePathRoots(input) {
      try {
        var parsed = JSON.parse(input.getAttribute('data-allowed-roots') || '[]');
        return Array.isArray(parsed) ? parsed : [];
      } catch (e) {
        return [];
      }
    }

    function expandSharePathHome(value, input) {
      var home = String(input.getAttribute('data-home-dir') || '');
      if (!home) return value;
      if (value === '~' || value.indexOf('~/') === 0) {
        return home + value.slice(1);
      }
      return value;
    }

    function isSharePathWithinRoots(value, roots) {
      if (!value) return false;
      var lower = value.toLowerCase();
      return roots.some(function (root) {
        var r = String(root).replace(/\/+$/, '').toLowerCase() || '/';
        return lower === r || lower.indexOf(r + '/') === 0;
      });
    }

    function syncPermissionWildcardExclusivity() {
      var form = find('#permissions-form');
      if (!form) return;
      var wildcard = form.querySelector('input[name="permissions"][data-wildcard]');
      if (!wildcard) return;
      form.querySelectorAll('input[name="permissions"]').forEach(function (box) {
        if (box !== wildcard) box.disabled = wildcard.checked;
      });
    }

    function updateApproveState() {
      var form = find('#permissions-form');
      if (!form) return;
      var approveBtn = find('#permissions-approve-btn');
      if (!approveBtn) return;
      syncPermissionWildcardExclusivity();
      var anyChecked = form.querySelector('input[name="permissions"]:checked') !== null;
      var pathInput = find('#file-sharing-path-input');
      var pathOk = true;
      if (pathInput) {
        var value = expandSharePathHome(pathInput.value.trim(), pathInput);
        var withinRoots = isSharePathWithinRoots(value, sharePathRoots(pathInput));
        pathOk = value.length > 0 && withinRoots;
        var hint = find('#file-sharing-path-hint');
        if (hint) hint.classList.toggle('hidden', !(value.length > 0 && !withinRoots));
      }
      approveBtn.disabled = !(anyChecked && pathOk);
    }

    function wireSharePathControls() {
      if (!(window.minds && window.minds.showFilePicker)) return;
      ['file-sharing-browse-file-btn', 'file-sharing-browse-folder-btn'].forEach(function (id) {
        var btn = find('#' + id);
        if (btn) btn.classList.remove('hidden');
      });
    }

    async function browseForSharePath(mode) {
      var input = find('#file-sharing-path-input');
      if (!input || !(window.minds && window.minds.showFilePicker)) return;
      try {
        var selected = await window.minds.showFilePicker({
          defaultPath: input.value.trim(),
          mode: mode === 'directory' ? 'directory' : 'file',
        });
        if (typeof selected === 'string' && selected.length > 0) {
          input.value = selected;
          updateApproveState();
        }
      } catch (e) {
        /* user cancelled or the bridge errored -- keep the current path */
      }
    }

    function applyEmptyState() {
      var hasCards = !!inboxList.querySelector('.inbox-card');
      if (hasCards) inboxBody.classList.remove('is-empty');
      else inboxBody.classList.add('is-empty');
    }

    function getSelectedId() {
      var card = inboxList.querySelector('.inbox-card.is-selected');
      return card ? card.getAttribute('data-request-id') : null;
    }

    var denyingIds = new Set();

    function isSelectableCard(el) {
      return !!el
        && el.classList
        && el.classList.contains('inbox-card')
        && !el.classList.contains('is-denying');
    }

    function syncDenyingClasses() {
      var pruned = new Set();
      denyingIds.forEach(function (id) {
        var card = inboxList.querySelector('.inbox-card[data-request-id="' + id + '"]');
        if (card) {
          card.classList.add('is-denying');
          pruned.add(id);
        }
      });
      denyingIds = pruned;
    }

    function findNextPendingId(resolvedId) {
      var current = inboxList.querySelector('.inbox-card[data-request-id="' + resolvedId + '"]');
      if (!current) {
        var any = inboxList.querySelector('.inbox-card:not(.is-denying)');
        return any ? any.getAttribute('data-request-id') : null;
      }
      var sibling = current.nextElementSibling;
      while (sibling && !isSelectableCard(sibling)) sibling = sibling.nextElementSibling;
      if (!sibling) {
        sibling = current.previousElementSibling;
        while (sibling && !isSelectableCard(sibling)) sibling = sibling.previousElementSibling;
      }
      return sibling ? sibling.getAttribute('data-request-id') : null;
    }

    async function fetchListFragment() {
      var resp = await fetch('/inbox/list', { credentials: 'same-origin' });
      if (!resp.ok) return;
      var html = await resp.text();
      inboxList.innerHTML = html;
      syncDenyingClasses();
      applyEmptyState();
    }

    async function fetchDetailFragment(id) {
      var resp = await fetch('/inbox/detail/' + encodeURIComponent(id), { credentials: 'same-origin' });
      if (!resp.ok) return;
      var html = await resp.text();
      inboxDetail.innerHTML = html;
      updateApproveState();
      wireSharePathControls();
    }

    function setSelectedCard(id) {
      inboxList.querySelectorAll('.inbox-card.is-selected').forEach(function (c) {
        c.classList.remove('is-selected');
      });
      if (!id) return;
      var card = inboxList.querySelector('.inbox-card[data-request-id="' + id + '"]');
      if (card) card.classList.add('is-selected');
    }

    function updateUrl(id) {
      try {
        var target = id ? '/inbox?selected=' + encodeURIComponent(id) : '/inbox';
        history.replaceState(null, '', target);
      } catch (e) { /* noop in restricted contexts */ }
    }

    async function selectItem(id) {
      setSelectedCard(id);
      updateUrl(id);
      if (id) await fetchDetailFragment(id);
    }

    async function advanceAfterResolution(resolvedId) {
      var nextId = findNextPendingId(resolvedId);
      await fetchListFragment();
      if (nextId) {
        var stillSelectable = inboxList.querySelector(
          '.inbox-card[data-request-id="' + nextId + '"]:not(.is-denying)',
        );
        if (!stillSelectable) {
          var fallback = inboxList.querySelector('.inbox-card:not(.is-denying)');
          nextId = fallback ? fallback.getAttribute('data-request-id') : null;
        }
      }
      if (nextId) {
        await selectItem(nextId);
      } else {
        closeInbox();
      }
    }

    function scrollDetailIntoView(el) {
      if (!el) return;
      try {
        inboxDetail.scrollTo({ top: inboxDetail.scrollHeight, behavior: 'smooth' });
      } catch (e) {
        inboxDetail.scrollTop = inboxDetail.scrollHeight;
      }
    }

    async function submitGrant(form, resolvedId) {
      var approveBtn = find('#permissions-approve-btn');
      var errorBox = find('#permissions-error');
      var errorMsg = find('#permissions-error-message');
      var manualBox = find('#permissions-manual-credentials');
      var progress = find('#permissions-progress');
      if (approveBtn) approveBtn.disabled = true;
      if (errorBox) errorBox.classList.add('hidden');
      if (manualBox) manualBox.classList.add('hidden');
      if (progress) {
        progress.classList.remove('hidden');
        scrollDetailIntoView(progress);
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
          throw new Error(text || ('HTTP ' + response.status));
        }
        var data = await response.json();
        if (data.outcome === 'GRANTED' || data.outcome === 'DENIED') {
          await advanceAfterResolution(resolvedId);
          return;
        }
        if (progress) progress.classList.add('hidden');
        if (data.outcome === 'NEEDS_MANUAL_CREDENTIALS') {
          var cmdEl = find('#permissions-manual-credentials-command');
          var msgEl = find('#permissions-manual-credentials-message');
          if (cmdEl) cmdEl.textContent = data.set_credentials_example || '';
          if (msgEl) msgEl.textContent = data.message || '';
          if (manualBox) {
            manualBox.classList.remove('hidden');
            scrollDetailIntoView(manualBox);
          }
          updateApproveState();
          return;
        }
        if (data.outcome === 'FAILED') {
          if (errorMsg) errorMsg.textContent = data.message || 'Approval failed; please try again.';
          if (errorBox) {
            errorBox.classList.remove('hidden');
            scrollDetailIntoView(errorBox);
          }
          if (approveBtn) approveBtn.disabled = false;
          return;
        }
        if (errorMsg) errorMsg.textContent = data.message || 'Authorization failed.';
        if (errorBox) {
          errorBox.classList.remove('hidden');
          scrollDetailIntoView(errorBox);
        }
        if (approveBtn) approveBtn.disabled = false;
      } catch (err) {
        if (progress) progress.classList.add('hidden');
        if (errorMsg) errorMsg.textContent = err && err.message ? err.message : String(err);
        if (errorBox) {
          errorBox.classList.remove('hidden');
          scrollDetailIntoView(errorBox);
        }
        if (approveBtn) approveBtn.disabled = false;
      }
    }

    function submitPermissionDeny() {
      var form = find('#permissions-form');
      if (!form) return;
      var resolvedId = getSelectedId();
      if (resolvedId) {
        denyingIds.add(resolvedId);
        var card = inboxList.querySelector('.inbox-card[data-request-id="' + resolvedId + '"]');
        if (card) card.classList.add('is-denying');
      }
      var approveBtn = find('#permissions-approve-btn');
      if (approveBtn) approveBtn.disabled = true;
      var denyUrl = form.action.replace(/\/grant\b/, '/deny');
      fetch(denyUrl, {
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
      }).catch(function () {});
      advanceAfterResolution(resolvedId);
    }

    // -- Event delegation (on container elements; dropped with the DOM) --

    inboxList.addEventListener('click', function (e) {
      var card = e.target.closest('.inbox-card');
      if (!card) return;
      var id = card.getAttribute('data-request-id');
      if (id) selectItem(id);
    });

    inboxDetail.addEventListener('submit', function (e) {
      var form = e.target;
      if (!form || form.id !== 'permissions-form') return;
      e.preventDefault();
      submitGrant(form, getSelectedId());
    });

    inboxDetail.addEventListener('change', function (e) {
      if (e.target && e.target.name === 'permissions') updateApproveState();
    });

    // -- Live list refresh from the host's cached chrome events (Electron only;
    // in the browser there is no MINDS_OVERLAY_HOST, so no subscription) --

    if (host.onChromeEvent) {
      teardownFns.push(host.onChromeEvent(function (evt) {
        if (!evt || evt.type !== 'requests') return;
        var currentId = getSelectedId();
        var newIds = Array.isArray(evt.request_ids) ? evt.request_ids.map(String) : [];
        fetchListFragment().then(function () {
          if (!currentId) return;
          if (newIds.indexOf(currentId) === -1) {
            fetchDetailFragment(currentId);
            updateUrl(null);
          } else {
            setSelectedCard(currentId);
          }
        });
      }));
    }

    // Standalone (browser) affordances: Electron's host owns the backdrop
    // click-outside dismiss and main owns Escape, so wire these only with no host.
    if (!isElectron) {
      var backdrop = find('#inbox-backdrop');
      if (backdrop) {
        backdrop.addEventListener('click', function (event) {
          if (event.target === backdrop) closeInbox();
        });
      }
      var onKeydown = function (event) { if (event.key === 'Escape') closeInbox(); };
      document.addEventListener('keydown', onKeydown);
      teardownFns.push(function () { document.removeEventListener('keydown', onKeydown); });
    }

    // Expose the handlers the server-rendered detail fragments call inline.
    window.closeInbox = closeInbox;
    window.onAutoOpenToggle = onAutoOpenToggle;
    window.showPermissionEditor = showPermissionEditor;
    window.updateApproveState = updateApproveState;
    window.browseForSharePath = browseForSharePath;
    window.submitPermissionDeny = submitPermissionDeny;

    applyEmptyState();
    updateApproveState();
    wireSharePathControls();

    return function teardown() {
      teardownFns.forEach(function (fn) { try { fn(); } catch (e) { /* noop */ } });
      GLOBAL_HANDLER_NAMES.forEach(function (name) { delete window[name]; });
    };
  }

  // Electron overlay registration.
  var teardown = null;
  window.MINDS_OVERLAY_MODALS.inbox = {
    positioning: 'backdrop',
    init: function (container) { teardown = initInbox(container); },
    destroy: function () {
      if (teardown) { try { teardown(); } catch (e) { /* noop */ } teardown = null; }
    },
  };

  // Standalone browser page: the inbox DOM is present at load, so wire it against
  // the document. No-op in the overlay host (no inbox DOM at load; the host drives
  // init via the registry when the modal opens).
  if (document.getElementById('inbox-body')) initInbox(document);
})();
