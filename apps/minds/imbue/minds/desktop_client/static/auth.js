// Sign-up / sign-in tab handling + OAuth polling. Tab switches via
// data-show-tab, OAuth via data-oauth. Keeps markup JS-free.
(function () {
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
        window.location.href = '/auth/check-email';
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
        if (data.needsEmailVerification) window.location.href = '/auth/check-email';
        else window.location.href = '/';
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

  function oauthShowWaiting(provider) {
    var nameMap = { google: 'Google', github: 'GitHub' };
    var providerLabel = nameMap[provider] || provider;
    document.querySelectorAll('.oauth-btn').forEach(function (b) { b.disabled = true; });
    var msg = 'Waiting for you to finish signing in with ' + providerLabel + ' in the browser...';
    ['signup-error', 'signin-error'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = msg;
      el.classList.remove('hidden');
      el.className = 'text-blue-900 text-sm mb-3 px-3 py-2 bg-blue-50 rounded-md border border-blue-100';
    });
  }

  async function oauthSignIn(provider) {
    var flowId = null;
    try {
      var res = await fetch('/auth/oauth/' + provider);
      var data = await res.json();
      if (data.status !== 'OK') {
        alert('Failed to start OAuth: ' + (data.error || data.message));
        return;
      }
      flowId = data.flow_id;
      if (!flowId) {
        alert('Failed to start OAuth: server did not return a flow_id');
        return;
      }
    } catch (err) {
      alert('Failed to start OAuth: ' + err.message);
      return;
    }
    oauthShowWaiting(provider);
    if (oauthPollInterval) clearInterval(oauthPollInterval);
    oauthPollDeadline = Date.now() + 3 * 60 * 1000;
    oauthPollInterval = setInterval(async function () {
      if (Date.now() > oauthPollDeadline) {
        clearInterval(oauthPollInterval);
        oauthPollInterval = null;
        document.querySelectorAll('.oauth-btn').forEach(function (b) { b.disabled = false; });
        alert('Sign-in timed out. Try again.');
        return;
      }
      try {
        var r = await fetch('/auth/oauth/status/' + flowId);
        var s = await r.json();
        if (s.status !== 'OK') {
          // Server forgot the flow (e.g. desktop server restart). Stop polling.
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          document.querySelectorAll('.oauth-btn').forEach(function (b) { b.disabled = false; });
          alert('Sign-in lost track of this flow. Try again.');
          return;
        }
        if (s.state === 'done') {
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          window.location.href = '/accounts';
          return;
        }
        if (s.state === 'error') {
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          document.querySelectorAll('.oauth-btn').forEach(function (b) { b.disabled = false; });
          alert('Sign-in failed: ' + (s.error || 'unknown error'));
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
