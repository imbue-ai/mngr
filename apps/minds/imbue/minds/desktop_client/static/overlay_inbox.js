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
    var teardownCallbacks = [];

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
      } catch (error) {
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
        var normalizedRoot = String(root).replace(/\/+$/, '').toLowerCase() || '/';
        return lower === normalizedRoot || lower.indexOf(normalizedRoot + '/') === 0;
      });
    }

    function syncPermissionWildcardExclusivity() {
      var form = find('#permissions-form');
      if (!form) return;
      var wildcard = form.querySelector('input[name="permissions"][data-wildcard]');
      if (!wildcard) return;
      form.querySelectorAll('input[name="permissions"]').forEach(function (checkbox) {
        if (checkbox !== wildcard) checkbox.disabled = wildcard.checked;
      });
    }

    function updateApproveState() {
      var form = find('#permissions-form');
      if (!form) return;
      var approveButton = find('#permissions-approve-btn');
      if (!approveButton) return;
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
      approveButton.disabled = !(anyChecked && pathOk);
    }

    function wireSharePathControls() {
      if (!(window.minds && window.minds.showFilePicker)) return;
      ['file-sharing-browse-file-btn', 'file-sharing-browse-folder-btn'].forEach(function (id) {
        var button = find('#' + id);
        if (button) button.classList.remove('hidden');
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
      } catch (error) {
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

    function isSelectableCard(element) {
      return !!element
        && element.classList
        && element.classList.contains('inbox-card')
        && !element.classList.contains('is-denying');
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
      var response = await fetch('/inbox/list', { credentials: 'same-origin' });
      if (!response.ok) return;
      var html = await response.text();
      inboxList.innerHTML = html;
      syncDenyingClasses();
      applyEmptyState();
    }

    async function fetchDetailFragment(id) {
      var response = await fetch('/inbox/detail/' + encodeURIComponent(id), { credentials: 'same-origin' });
      if (!response.ok) return;
      var html = await response.text();
      inboxDetail.innerHTML = html;
      updateApproveState();
      wireSharePathControls();
    }

    function setSelectedCard(id) {
      inboxList.querySelectorAll('.inbox-card.is-selected').forEach(function (selectedCard) {
        selectedCard.classList.remove('is-selected');
      });
      if (!id) return;
      var card = inboxList.querySelector('.inbox-card[data-request-id="' + id + '"]');
      if (card) card.classList.add('is-selected');
    }

    function updateUrl(id) {
      try {
        var target = id ? '/inbox?selected=' + encodeURIComponent(id) : '/inbox';
        history.replaceState(null, '', target);
      } catch (error) { /* noop in restricted contexts */ }
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

    function scrollDetailIntoView(element) {
      if (!element) return;
      try {
        inboxDetail.scrollTo({ top: inboxDetail.scrollHeight, behavior: 'smooth' });
      } catch (error) {
        inboxDetail.scrollTop = inboxDetail.scrollHeight;
      }
    }

    async function submitGrant(form, resolvedId) {
      var approveButton = find('#permissions-approve-btn');
      var errorBox = find('#permissions-error');
      var errorMessageElement = find('#permissions-error-message');
      var manualBox = find('#permissions-manual-credentials');
      var progress = find('#permissions-progress');
      if (approveButton) approveButton.disabled = true;
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
          var commandElement = find('#permissions-manual-credentials-command');
          var messageElement = find('#permissions-manual-credentials-message');
          if (commandElement) commandElement.textContent = data.set_credentials_example || '';
          if (messageElement) messageElement.textContent = data.message || '';
          if (manualBox) {
            manualBox.classList.remove('hidden');
            scrollDetailIntoView(manualBox);
          }
          updateApproveState();
          return;
        }
        if (data.outcome === 'FAILED') {
          if (errorMessageElement) errorMessageElement.textContent = data.message || 'Approval failed; please try again.';
          if (errorBox) {
            errorBox.classList.remove('hidden');
            scrollDetailIntoView(errorBox);
          }
          if (approveButton) approveButton.disabled = false;
          return;
        }
        if (errorMessageElement) errorMessageElement.textContent = data.message || 'Authorization failed.';
        if (errorBox) {
          errorBox.classList.remove('hidden');
          scrollDetailIntoView(errorBox);
        }
        if (approveButton) approveButton.disabled = false;
      } catch (error) {
        if (progress) progress.classList.add('hidden');
        if (errorMessageElement) errorMessageElement.textContent = error && error.message ? error.message : String(error);
        if (errorBox) {
          errorBox.classList.remove('hidden');
          scrollDetailIntoView(errorBox);
        }
        if (approveButton) approveButton.disabled = false;
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
      var approveButton = find('#permissions-approve-btn');
      if (approveButton) approveButton.disabled = true;
      var denyUrl = form.action.replace(/\/grant\b/, '/deny');
      fetch(denyUrl, {
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
      }).catch(function () {});
      advanceAfterResolution(resolvedId);
    }

    // -- Event delegation (on container elements; dropped with the DOM) --

    inboxList.addEventListener('click', function (event) {
      var card = event.target.closest('.inbox-card');
      if (!card) return;
      var id = card.getAttribute('data-request-id');
      if (id) selectItem(id);
    });

    inboxDetail.addEventListener('submit', function (event) {
      var form = event.target;
      if (!form || form.id !== 'permissions-form') return;
      event.preventDefault();
      submitGrant(form, getSelectedId());
    });

    inboxDetail.addEventListener('change', function (event) {
      if (event.target && event.target.name === 'permissions') updateApproveState();
    });

    // -- Live list refresh from the host's cached chrome events (Electron only;
    // in the browser there is no MINDS_OVERLAY_HOST, so no subscription) --

    if (host.onChromeEvent) {
      teardownCallbacks.push(host.onChromeEvent(function (event) {
        if (!event || event.type !== 'requests') return;
        var currentId = getSelectedId();
        var newIds = Array.isArray(event.request_ids) ? event.request_ids.map(String) : [];
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
      teardownCallbacks.push(function () { document.removeEventListener('keydown', onKeydown); });
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
      teardownCallbacks.forEach(function (callback) { try { callback(); } catch (error) { /* noop */ } });
      GLOBAL_HANDLER_NAMES.forEach(function (name) { delete window[name]; });
    };
  }

  // Electron overlay registration.
  var teardown = null;
  window.MINDS_OVERLAY_MODALS.inbox = {
    positioning: 'backdrop',
    init: function (container) { teardown = initInbox(container); },
    destroy: function () {
      if (teardown) { try { teardown(); } catch (error) { /* noop */ } teardown = null; }
    },
  };

  // Standalone browser page: the inbox DOM is present at load, so wire it against
  // the document. No-op in the overlay host (no inbox DOM at load; the host drives
  // init via the registry when the modal opens).
  if (document.getElementById('inbox-body')) initInbox(document);
})();
