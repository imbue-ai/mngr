// The Associate prompt's account picker (templates/Associate.jinja).
//
// This lives in a real static file rather than an inline <script> in the
// component because the shell's page-swap engine only re-executes scripts from
// #local-page-scripts (see swapLocalPage in chrome.js). An inline script inside
// the swapped page body is adopted, not re-run, so on every surface reached by
// a swap the form would render with no submit handler at all -- and a <form>
// with no handler and no action does a native GET, which looks like the button
// doing nothing while quietly dropping the page's query params.
//
// Binds every prompt on the page independently, so a surface that renders more
// than one (the options panel shows the Share pane's and Machine settings'
// account group at once) gets a working handler on each.

(function () {
  'use strict';

  var ADD_ACCOUNT_VALUE = '__add_account__';
  var AUTH_LOGIN_PATH = '/auth/login';
  var SIGNIN_RETURN_PATH = '/';

  function bindPrompt(root) {
    var form = root.querySelector('[data-associate-form]');
    if (!form) return;
    var agentId = root.dataset.agentId;
    var redirectUrl = root.dataset.redirectUrl || '';
    var errorEl = root.querySelector('[data-associate-error]');
    var select = form.querySelector('[name="user_id"]');
    var submitBtn = form.querySelector('button[type="submit"], button');

    // Picking "Add account" is not a choice of account -- it starts a sign-in.
    // The shell has that as a modal, so the surrounding page stays put; the
    // full-page auth flow is the browser-mode fallback. Snap the picker back so
    // it never rests on an entry that cannot be submitted.
    var lastRealValue = select ? select.value : '';
    if (select) {
      select.addEventListener('change', function () {
        if (select.value !== ADD_ACCOUNT_VALUE) {
          lastRealValue = select.value;
          return;
        }
        select.value = lastRealValue;
        if (window.minds && window.minds.openSigninModal) window.minds.openSigninModal(SIGNIN_RETURN_PATH);
        else window.location.href = AUTH_LOGIN_PATH;
      });
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var accountId = select && select.value;
      if (!accountId || accountId === ADD_ACCOUNT_VALUE) return;
      // The button reports its own wait: "Linking..." plus a spinner, in
      // place of its label. The inverse tone keeps the spinner legible on the
      // primary variant's solid fill.
      window.mindsButtonBusy.set(submitBtn, 'Linking...', 'inverse');
      if (errorEl) errorEl.classList.add('hidden');
      fetch('/api/v1/workspaces/' + encodeURIComponent(agentId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_id: accountId }),
      })
        .then(function (response) {
          if (response.ok) {
            if (redirectUrl) window.location.href = redirectUrl;
            else window.location.reload();
            return null;
          }
          // The server says WHY it refused (an unverified email, an expired
          // session); reporting only the status code sends the user hunting
          // for a fault that is not theirs to find.
          return response.text().then(function (text) {
            var detail = '';
            try {
              var parsed = JSON.parse(text);
              detail = parsed.error || parsed.detail || parsed.message || '';
            } catch (parseError) {
              detail = text;
            }
            throw new Error(detail || 'HTTP ' + response.status);
          });
        })
        .catch(function (error) {
          window.mindsButtonBusy.clear(submitBtn);
          if (errorEl) {
            errorEl.textContent = 'Could not link the account: ' + error.message;
            errorEl.classList.remove('hidden');
          }
        });
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-associate]'), bindPrompt);
})();
