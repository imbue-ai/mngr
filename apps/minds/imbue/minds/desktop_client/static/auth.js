// Sign-up / sign-in tab handling + OAuth polling. Tab switches via
// data-show-tab, OAuth via data-oauth. Keeps markup JS-free.
(function () {
  // Where to land after a successful sign-in. When the page carries a
  // ``return_to`` query param (e.g. the create page sent a signed-out user
  // here to enable the remote preset), forward it to /post-login so the
  // server returns them there; /post-login re-validates it as a safe path.
  function postLoginUrl() {
    var returnTo = new URLSearchParams(window.location.search).get('return_to');
    return returnTo ? '/post-login?return_to=' + encodeURIComponent(returnTo) : '/post-login';
  }

  // How to perform a post-auth navigation. The standalone auth page just
  // navigates this page (window.location). When this form is hosted in the
  // create screen's sign-in modal -- its own WebContentsView in the desktop
  // client's overlay layer -- the host page sets ``window.MINDS_AUTH_NAV`` to
  // route the navigation to the content view *behind* the modal and dismiss the
  // overlay; reloading this page would only reload the overlay.
  function authNavigate(url) {
    if (typeof window.MINDS_AUTH_NAV === 'function') window.MINDS_AUTH_NAV(url);
    else window.location.href = url;
  }

  // What to do after a successful sign-in / OAuth. The sign-in modal sets
  // ``window.MINDS_AUTH_RETURN_TO`` to the create screen so the user lands back
  // there signed in (and clicks "Create" again); the standalone auth page has
  // no such hint and goes through /post-login (which may carry its own
  // ?return_to=).
  function onAuthSuccess() {
    authNavigate(window.MINDS_AUTH_RETURN_TO || postLoginUrl());
  }

  // Where to return after an email-verification round-trip (sign-up, or
  // sign-in of an unverified account). The standalone auth page honors its
  // ``?return_to=`` query param; the sign-in modal sets
  // ``window.MINDS_AUTH_RETURN_TO`` (e.g. /create) so the user lands back in
  // the create flow rather than on the accounts page. The path is carried
  // through /auth/check-email -> /post-login, which re-validates it as a safe
  // path.
  function verificationReturnTo() {
    var q = new URLSearchParams(window.location.search).get('return_to');
    if (q) return q;
    return window.MINDS_AUTH_RETURN_TO || null;
  }

  function goToCheckEmail() {
    var rt = verificationReturnTo();
    authNavigate('/auth/check-email' + (rt ? '?return_to=' + encodeURIComponent(rt) : ''));
  }

  function showTab(tab) {
    document.getElementById('signup-tab').classList.toggle('hidden', tab !== 'signup');
    document.getElementById('signin-tab').classList.toggle('hidden', tab !== 'signin');
  }

  function showError(prefix, msg) {
    var el = document.getElementById(prefix + '-error');
    el.textContent = msg;
    el.classList.remove('hidden');
  }

  async function handleSignup(e) {
    e.preventDefault();
    var btn = document.getElementById('signup-btn');
    btn.disabled = true;
    btn.textContent = 'Creating account...';
    document.getElementById('signup-error').classList.add('hidden');
    try {
      var res = await fetch('/auth/api/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('signup-email').value,
          password: document.getElementById('signup-password').value,
        }),
      });
      var data = await res.json();
      if (data.status === 'OK') {
        goToCheckEmail();
      } else if (data.status === 'EMAIL_ALREADY_EXISTS' || data.status === 'FIELD_ERROR') {
        showError('signup', data.message);
      } else {
        showError('signup', data.message || 'Sign-up failed');
      }
    } catch (err) {
      showError('signup', 'Network error: ' + err.message);
    }
    btn.disabled = false;
    btn.textContent = 'Create account';
    return false;
  }

  async function handleSignin(e) {
    e.preventDefault();
    var btn = document.getElementById('signin-btn');
    btn.disabled = true;
    btn.textContent = 'Signing in...';
    document.getElementById('signin-error').classList.add('hidden');
    try {
      var res = await fetch('/auth/api/signin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: document.getElementById('signin-email').value,
          password: document.getElementById('signin-password').value,
        }),
      });
      var data = await res.json();
      if (data.status === 'OK') {
        if (data.needsEmailVerification) goToCheckEmail();
        else onAuthSuccess();
      } else if (data.status === 'WRONG_CREDENTIALS') {
        showError('signin', data.message);
      } else {
        showError('signin', data.message || 'Sign-in failed');
      }
    } catch (err) {
      showError('signin', 'Network error: ' + err.message);
    }
    btn.disabled = false;
    btn.textContent = 'Sign in';
    return false;
  }

  var oauthPollInterval = null;
  var oauthPollDeadline = 0;

  var OAUTH_PROVIDER_LABELS = { google: 'Google', github: 'GitHub' };

  // The two shared classNames for the status box (the "blue box"). The waiting
  // variant carries the staged progress messages; the error variant matches
  // ``Notice variant="error"`` so a failure reads the same as every other
  // in-page error. Kept as literals so Tailwind's source scan emits them.
  var OAUTH_STATUS_CLASS = 'text-accent type-body mb-3 px-3 py-2 bg-accent/12 rounded-md border border-accent/30';
  var OAUTH_ERROR_CLASS = 'text-important type-body mb-3 px-3 py-2 bg-[var(--c-important-surface)] rounded-md';

  // The status box is the (repurposed) error Notice on whichever tab is
  // visible; update both so it shows regardless of which one the user is on.
  function oauthSetMessage(msg, className) {
    ['signup-error', 'signin-error'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = msg;
      el.className = className;
      el.classList.remove('hidden');
    });
  }

  // Fade + disable every OAuth button for the duration of the flow. On the
  // button whose provider the user clicked, swap its brand icon for the spinner
  // (same 18px slot, so the button doesn't change width); the others keep their
  // icon and just dim.
  function oauthSetButtonsBusy(provider) {
    document.querySelectorAll('.oauth-btn').forEach(function (b) {
      b.disabled = true;
      b.classList.add('opacity-60');
      var isClicked = b.getAttribute('data-oauth') === provider;
      var spinner = b.querySelector('.oauth-btn-spinner');
      var icon = b.querySelector('.oauth-btn-icon');
      if (spinner) spinner.classList.toggle('hidden', !isClicked);
      if (icon) icon.classList.toggle('hidden', isClicked);
    });
  }

  function oauthResetButtons() {
    document.querySelectorAll('.oauth-btn').forEach(function (b) {
      b.disabled = false;
      b.classList.remove('opacity-60');
      var spinner = b.querySelector('.oauth-btn-spinner');
      var icon = b.querySelector('.oauth-btn-icon');
      if (spinner) spinner.classList.add('hidden');
      if (icon) icon.classList.remove('hidden');
    });
  }

  // Terminal failure: un-fade the buttons, stop the spinner, and surface the
  // reason in the status box (styled as an error) instead of a browser alert().
  function oauthFail(msg) {
    oauthResetButtons();
    oauthSetMessage(msg, OAUTH_ERROR_CLASS);
  }

  // Sign-in just completed in the external browser, which stole OS focus. Ask
  // the shell to raise the Minds window so the user doesn't have to alt-tab
  // back. On the standalone /auth page (content view) there is no window.minds
  // bridge, so we post an allowlisted message the content-relay preload
  // forwards; in the sign-in modal (overlay view) the bridge is present.
  function requestWindowFocus() {
    try {
      if (window.minds && typeof window.minds.focusWindow === 'function') {
        window.minds.focusWindow();
      } else {
        window.postMessage({ type: 'minds:focus-window' }, '*');
      }
    } catch (e) { /* focus is best-effort; never block sign-in on it */ }
  }

  async function oauthSignIn(provider) {
    var providerLabel = OAUTH_PROVIDER_LABELS[provider] || provider;
    // Immediate feedback the moment the button is clicked, before the browser
    // has even been asked to open.
    oauthSetButtonsBusy(provider);
    oauthSetMessage('Opening your browser...', OAUTH_STATUS_CLASS);
    var flowId = null;
    try {
      var res = await fetch('/auth/oauth/' + provider);
      var data = await res.json();
      if (data.status !== 'OK') {
        oauthFail('Could not start sign-in: ' + (data.error || data.message || 'unknown error'));
        return;
      }
      flowId = data.flow_id;
      if (!flowId) {
        oauthFail('Could not start sign-in: the server did not return a flow id.');
        return;
      }
    } catch (err) {
      oauthFail('Could not start sign-in: ' + err.message);
      return;
    }
    // The flow is live and the browser is up: now we are genuinely waiting on
    // the user to finish in the browser.
    oauthSetMessage('Waiting for you to finish signing in with ' + providerLabel + ' in the browser...', OAUTH_STATUS_CLASS);
    if (oauthPollInterval) clearInterval(oauthPollInterval);
    oauthPollDeadline = Date.now() + 3 * 60 * 1000;
    oauthPollInterval = setInterval(async function () {
      if (Date.now() > oauthPollDeadline) {
        clearInterval(oauthPollInterval);
        oauthPollInterval = null;
        oauthFail('Sign-in timed out. Try again.');
        return;
      }
      try {
        var r = await fetch('/auth/oauth/status/' + flowId);
        var s = await r.json();
        if (s.status !== 'OK') {
          // Server forgot the flow (e.g. desktop server restart). Stop polling.
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          oauthFail('Sign-in lost track of this flow. Try again.');
          return;
        }
        if (s.state === 'done') {
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          // Final step before the page navigates onward, plus the window raise
          // so the user lands back in Minds rather than the browser.
          oauthSetMessage('Signing you in...', OAUTH_STATUS_CLASS);
          requestWindowFocus();
          onAuthSuccess();
          return;
        }
        if (s.state === 'error') {
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          oauthFail('Sign-in failed: ' + (s.error || 'unknown error'));
          return;
        }
        // state === 'running' -- keep polling.
      } catch (e) { /* transient network blip; keep polling */ }
    }, 2000);
  }

  document.addEventListener('click', function (e) {
    var tabLink = e.target.closest('[data-show-tab]');
    if (tabLink) { e.preventDefault(); showTab(tabLink.getAttribute('data-show-tab')); return; }
    var oauthBtn = e.target.closest('[data-oauth]');
    if (oauthBtn && !oauthBtn.disabled) { oauthSignIn(oauthBtn.getAttribute('data-oauth')); }
  });

  var signupForm = document.getElementById('signup-form');
  if (signupForm) signupForm.addEventListener('submit', handleSignup);
  var signinForm = document.getElementById('signin-form');
  if (signinForm) signinForm.addEventListener('submit', handleSignin);
})();
