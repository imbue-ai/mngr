"""HTML templates for SuperTokens authentication pages.

Uses the same inline HTML pattern as templates.py. All pages use vanilla JS
with fetch() calls -- no React, no frontend SDK, no bundler.
"""

from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape

from imbue.minds.desktop_client.templates import TOKENS

_JINJA_ENV: Final[Environment] = Environment(autoescape=select_autoescape(default=True))

# Auth-specific overrides: a centered card with its own fixed width, and the
# OAuth button row. Everything else comes from the shared design tokens.
_AUTH_STYLES: Final[str] = (
    TOKENS
    + """
    body {
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh; padding: 20px;
    }
    .auth-card {
      background: var(--bg-surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-card);
      padding: 40px;
      box-shadow: var(--shadow-card);
      max-width: 420px; width: 100%;
    }
    .auth-card h1 { font-size: var(--fs-2xl); margin-bottom: 8px; }
    .auth-card .subtitle { color: var(--text-muted); font-size: var(--fs-md); margin-bottom: 24px; }
    .form-group { margin-bottom: 16px; }
    .form-group label {
      display: block; font-size: var(--fs-sm); font-weight: 500;
      color: var(--text); margin-bottom: 6px;
    }
    .form-group input {
      width: 100%; padding: 10px 12px;
      border: 1px solid var(--border); border-radius: var(--radius-lg);
      font-size: var(--fs-md); font-family: inherit; outline: none;
      background: var(--bg-surface); color: var(--text);
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .form-group input:focus { border-color: var(--link); box-shadow: var(--focus-ring); }
    .btn-primary {
      width: 100%; padding: 12px;
      border-radius: var(--radius-lg);
      font-size: var(--fs-md); font-weight: 600;
    }
    .divider {
      display: flex; align-items: center; margin: 20px 0;
      color: var(--text-subtle); font-size: var(--fs-xs);
    }
    .divider::before, .divider::after { content: ''; flex: 1; border-bottom: 1px solid var(--border); }
    .divider span { padding: 0 12px; }
    .oauth-btn {
      width: 100%; padding: 10px 12px;
      background: var(--bg-surface); border: 1px solid var(--border);
      border-radius: var(--radius-lg); font-size: var(--fs-md); cursor: pointer;
      font-family: inherit;
      display: flex; align-items: center; justify-content: center; gap: 8px;
      color: var(--text); transition: background 0.15s; margin-bottom: 8px;
    }
    .oauth-btn:hover { background: var(--bg-muted); }
    .oauth-btn svg { width: 18px; height: 18px; }
    .toggle-link {
      text-align: center; margin-top: 20px; font-size: var(--fs-sm); color: var(--text-muted);
    }
    .toggle-link a { color: var(--link); font-weight: 500; }
    .forgot-link { text-align: right; margin-top: -8px; margin-bottom: 16px; }
    .forgot-link a { font-size: var(--fs-xs); color: var(--link); }
    .error-msg {
      color: var(--danger); font-size: var(--fs-sm); margin-bottom: 12px;
      padding: 8px 12px; background: var(--danger-bg); border-radius: var(--radius);
      display: none;
    }
    .info-msg {
      color: #1e40af; font-size: var(--fs-sm); margin-bottom: 12px;
      padding: 8px 12px; background: #eff6ff; border-radius: var(--radius);
    }
    .success-msg {
      color: var(--success); font-size: var(--fs-sm); margin-bottom: 12px;
      padding: 8px 12px; background: var(--success-bg); border-radius: var(--radius);
      display: none;
    }
"""
)

_GOOGLE_SVG: Final[str] = (
    '<svg viewBox="0 0 24 24"><path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/><path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/><path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/><path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/></svg>'
)

_GITHUB_SVG: Final[str] = (
    '<svg viewBox="0 0 24 24" fill="#333"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>'
)


_AUTH_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>{{ title }}</title>
  <style>"""
    + _AUTH_STYLES
    + """
    .tab-hidden { display: none; }
  </style>
</head>
<body>
  <div class="auth-card">
    {% if message %}
    <div class="info-msg">{{ message }}</div>
    {% endif %}

    <div id="signup-tab" class="{{ 'tab-hidden' if not default_to_signup else '' }}">
      <h1>Create account</h1>
      <p class="subtitle">Sign up to enable sharing</p>
      <div id="signup-error" class="error-msg"></div>
      <form id="signup-form" onsubmit="return handleSignup(event)">
        <div class="form-group">
          <label for="signup-email">Email</label>
          <input type="email" id="signup-email" name="email" required autocomplete="email">
        </div>
        <div class="form-group">
          <label for="signup-password">Password</label>
          <input type="password" id="signup-password" name="password" required minlength="8" autocomplete="new-password">
        </div>
        <button type="submit" class="btn btn-primary" id="signup-btn">Create account</button>
      </form>
      <div class="divider"><span>or</span></div>
      <button class="oauth-btn" onclick="oauthSignIn('google')">"""
    + _GOOGLE_SVG
    + """ Continue with Google</button>
      <button class="oauth-btn" onclick="oauthSignIn('github')">"""
    + _GITHUB_SVG
    + """ Continue with GitHub</button>
      <div class="toggle-link">Already have an account? <a href="#" onclick="showTab('signin'); return false;">Sign in</a></div>
    </div>

    <div id="signin-tab" class="{{ 'tab-hidden' if default_to_signup else '' }}">
      <h1>Sign in</h1>
      <p class="subtitle">Sign in to your Imbue account</p>
      <div id="signin-error" class="error-msg"></div>
      <form id="signin-form" onsubmit="return handleSignin(event)">
        <div class="form-group">
          <label for="signin-email">Email</label>
          <input type="email" id="signin-email" name="email" required autocomplete="email">
        </div>
        <div class="form-group">
          <label for="signin-password">Password</label>
          <input type="password" id="signin-password" name="password" required autocomplete="current-password">
        </div>
        <div class="forgot-link"><a href="/auth/forgot-password">Forgot password?</a></div>
        <button type="submit" class="btn btn-primary" id="signin-btn">Sign in</button>
      </form>
      <div class="divider"><span>or</span></div>
      <button class="oauth-btn" onclick="oauthSignIn('google')">"""
    + _GOOGLE_SVG
    + """ Continue with Google</button>
      <button class="oauth-btn" onclick="oauthSignIn('github')">"""
    + _GITHUB_SVG
    + """ Continue with GitHub</button>
      <div class="toggle-link">Don't have an account? <a href="#" onclick="showTab('signup'); return false;">Create one</a></div>
    </div>
  </div>

  <script>
  function showTab(tab) {
    document.getElementById('signup-tab').classList.toggle('tab-hidden', tab !== 'signup');
    document.getElementById('signin-tab').classList.toggle('tab-hidden', tab !== 'signin');
  }

  function showError(prefix, msg) {
    var el = document.getElementById(prefix + '-error');
    el.textContent = msg;
    el.style.display = 'block';
  }

  async function handleSignup(e) {
    e.preventDefault();
    var btn = document.getElementById('signup-btn');
    btn.disabled = true;
    btn.textContent = 'Creating account...';
    document.getElementById('signup-error').style.display = 'none';

    try {
      var res = await fetch('/auth/api/signup', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          email: document.getElementById('signup-email').value,
          password: document.getElementById('signup-password').value
        })
      });
      var data = await res.json();
      if (data.status === 'OK') {
        window.location.href = '/auth/check-email';
      } else if (data.status === 'EMAIL_ALREADY_EXISTS') {
        showError('signup', data.message);
      } else if (data.status === 'FIELD_ERROR') {
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
    document.getElementById('signin-error').style.display = 'none';

    try {
      var res = await fetch('/auth/api/signin', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          email: document.getElementById('signin-email').value,
          password: document.getElementById('signin-password').value
        })
      });
      var data = await res.json();
      if (data.status === 'OK') {
        if (data.needsEmailVerification) {
          window.location.href = '/auth/check-email';
        } else {
          window.location.href = '/';
        }
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
    var buttons = document.querySelectorAll('.oauth-btn');
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = true; }
    var msg = 'Waiting for you to finish signing in with ' + providerLabel + ' in the browser...';
    var banners = [
      document.getElementById('signup-error'),
      document.getElementById('signin-error'),
    ];
    for (var j = 0; j < banners.length; j++) {
      var el = banners[j];
      if (!el) continue;
      el.textContent = msg;
      el.style.color = '#1e40af';
      el.style.background = '#eff6ff';
      el.style.display = 'block';
    }
  }

  async function oauthSignIn(provider) {
    var startBtn = event && event.target;
    if (startBtn && startBtn.disabled) return;
    try {
      var res = await fetch('/auth/oauth/' + provider);
      var data = await res.json();
      if (data.status !== 'OK') {
        alert('Failed to start OAuth: ' + (data.error || data.message));
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
        var buttons = document.querySelectorAll('.oauth-btn');
        for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = false; }
        alert('Sign-in timed out. Try again.');
        return;
      }
      try {
        var r = await fetch('/auth/api/status');
        var s = await r.json();
        if (s.signedIn) {
          clearInterval(oauthPollInterval);
          oauthPollInterval = null;
          window.location.href = '/accounts';
        }
      } catch (e) { /* transient -- keep polling */ }
    }, 2000);
  }
  </script>
</body>
</html>"""
)


_CHECK_EMAIL_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Check your email</title>
  <style>"""
    + _AUTH_STYLES
    + """</style>
</head>
<body>
  <div class="auth-card" style="text-align: center;">
    <h1>Check your email</h1>
    <p class="subtitle">We sent a verification link to <strong>{{ email }}</strong></p>
    <p class="muted" style="font-size: var(--fs-sm); margin-bottom: 20px;">
      Click the link in the email to verify your account, then come back here.
    </p>
    <div id="status-msg" class="info-msg">Waiting for verification...</div>
    <div id="success-msg" class="success-msg">Email verified. Redirecting...</div>
    <button class="btn btn-primary" style="margin-top: 16px;" onclick="resendEmail()" id="resend-btn">
      Resend verification email
    </button>
    <div class="toggle-link" style="margin-top: 12px;">
      <a href="/">Go to home</a>
    </div>
  </div>
  <script>
  var pollInterval = setInterval(async function() {
    try {
      var res = await fetch('/auth/api/email-verified');
      var data = await res.json();
      if (data.verified) {
        clearInterval(pollInterval);
        document.getElementById('status-msg').style.display = 'none';
        var s = document.getElementById('success-msg');
        s.style.display = 'block';
        setTimeout(function() { window.location.href = '/'; }, 1500);
      }
    } catch (e) {}
  }, 3000);

  async function resendEmail() {
    var btn = document.getElementById('resend-btn');
    btn.disabled = true;
    btn.textContent = 'Sending...';
    try {
      await fetch('/auth/api/resend-verification', { method: 'POST' });
      btn.textContent = 'Sent. Check your inbox';
      setTimeout(function() {
        btn.disabled = false;
        btn.textContent = 'Resend verification email';
      }, 5000);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Resend verification email';
    }
  }
  </script>
</body>
</html>"""
)


_OAUTH_CLOSE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Signed in</title>
  <style>"""
    + _AUTH_STYLES
    + """</style>
</head>
<body>
  <div class="auth-card" style="text-align: center;">
    <h1>Signed in</h1>
    <p class="subtitle">Signed in as <strong>{{ display_name or email }}</strong></p>
    <p class="muted" style="font-size: var(--fs-sm);">You can close this tab and return to the app.</p>
  </div>
</body>
</html>"""
)


_FORGOT_PASSWORD_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Forgot password</title>
  <style>"""
    + _AUTH_STYLES
    + """</style>
</head>
<body>
  <div class="auth-card">
    <h1>Reset password</h1>
    <p class="subtitle">Enter your email to receive a reset link</p>
    <div id="error-msg" class="error-msg"></div>
    <div id="success-msg" class="success-msg"></div>
    <form onsubmit="return handleForgot(event)">
      <div class="form-group">
        <label for="email">Email</label>
        <input type="email" id="email" required autocomplete="email">
      </div>
      <button type="submit" class="btn btn-primary" id="submit-btn">Send reset link</button>
    </form>
    <div class="toggle-link"><a href="/auth/login">Back to sign in</a></div>
  </div>
  <script>
  async function handleForgot(e) {
    e.preventDefault();
    var btn = document.getElementById('submit-btn');
    btn.disabled = true;
    document.getElementById('error-msg').style.display = 'none';
    try {
      var res = await fetch('/auth/api/forgot-password', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ email: document.getElementById('email').value })
      });
      var data = await res.json();
      var s = document.getElementById('success-msg');
      s.textContent = data.message;
      s.style.display = 'block';
    } catch (err) {
      var el = document.getElementById('error-msg');
      el.textContent = 'Network error';
      el.style.display = 'block';
    }
    btn.disabled = false;
  }
  </script>
</body>
</html>"""
)


_SETTINGS_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Account settings</title>
  <style>"""
    + _AUTH_STYLES
    + """
    .auth-card { max-width: 480px; }
    .detail-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 12px 0; border-bottom: 1px solid var(--bg-muted);
    }
    .detail-label { font-size: var(--fs-sm); color: var(--text-muted); }
    .detail-value { font-size: var(--fs-md); color: var(--text); font-weight: 500; }
    .auth-actions { margin-top: 24px; display: flex; flex-direction: column; gap: 8px; }
    .auth-actions .btn { width: 100%; padding: 10px; border-radius: var(--radius-lg); font-size: var(--fs-md); }
  </style>
</head>
<body>
  <div class="auth-card">
    <h1>Account settings</h1>
    <p class="subtitle">Manage your Imbue account</p>
    <div class="detail-row">
      <span class="detail-label">Email</span>
      <span class="detail-value">{{ email }}</span>
    </div>
    {% if display_name %}
    <div class="detail-row">
      <span class="detail-label">Name</span>
      <span class="detail-value">{{ display_name }}</span>
    </div>
    {% endif %}
    <div class="detail-row">
      <span class="detail-label">Auth provider</span>
      <span class="detail-value">{{ provider }}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">User ID prefix</span>
      <span class="detail-value" style="font-family: var(--font-mono); font-size: var(--fs-xs);">{{ user_id_prefix }}</span>
    </div>
    <div class="auth-actions">
      {% if provider == 'email' %}
      <a href="/auth/forgot-password" class="btn btn-secondary" style="text-align: center;">Change password</a>
      {% endif %}
      <button class="btn btn-danger" onclick="signOut()">Sign out</button>
    </div>
    <div class="toggle-link"><a href="/">Back to home</a></div>
  </div>
  <script>
  async function signOut() {
    await fetch('/auth/api/signout', { method: 'POST' });
    window.location.href = '/';
  }
  </script>
</body>
</html>"""
)


def render_auth_page(
    default_to_signup: bool = True,
    message: str | None = None,
    server_port: int = 0,
) -> str:
    """Render the sign-up / sign-in page."""
    template = _JINJA_ENV.from_string(_AUTH_PAGE_TEMPLATE)
    title = "Create account" if default_to_signup else "Sign in"
    return template.render(
        title=title,
        default_to_signup=default_to_signup,
        message=message,
        server_port=server_port,
    )


def render_check_email_page(email: str) -> str:
    """Render the 'check your email for verification' page."""
    template = _JINJA_ENV.from_string(_CHECK_EMAIL_TEMPLATE)
    return template.render(email=email)


def render_oauth_close_page(email: str, display_name: str | None = None) -> str:
    """Render the 'you can close this tab' page after OAuth."""
    template = _JINJA_ENV.from_string(_OAUTH_CLOSE_TEMPLATE)
    return template.render(email=email, display_name=display_name)


def render_forgot_password_page() -> str:
    """Render the forgot password page."""
    template = _JINJA_ENV.from_string(_FORGOT_PASSWORD_TEMPLATE)
    return template.render()


def render_settings_page(
    email: str,
    display_name: str | None,
    user_id: str,
    provider: str,
    user_id_prefix: str,
) -> str:
    """Render the account settings page."""
    template = _JINJA_ENV.from_string(_SETTINGS_TEMPLATE)
    return template.render(
        email=email,
        display_name=display_name,
        user_id=user_id,
        provider=provider,
        user_id_prefix=user_id_prefix,
    )
