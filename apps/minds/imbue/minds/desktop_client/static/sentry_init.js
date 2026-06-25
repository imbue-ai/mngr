// Boot the frontend (browser) Sentry SDK for the minds web UI.
//
// This runs synchronously in <head>, immediately after the vendored
// `sentry.browser.min.js` bundle (see Base.jinja), so unhandled errors thrown
// later while the page parses/executes are captured. The backend only emits
// these scripts when frontend reporting is enabled and a real DSN is configured
// for the environment (see imbue/minds/utils/sentry/frontend.py); the config is
// passed in as a JSON <script> blob rather than inline JS.
//
// To refresh the vendored bundle, re-download the matching pinned version from
// https://browser.sentry-cdn.com/<version>/bundle.min.js into
// static/sentry.browser.min.js (kept in sync with the backend @sentry/* SDK
// version) and update the changelog.
(function () {
  try {
    var configElement = document.getElementById('minds-sentry-config');
    if (!configElement || !window.Sentry) {
      return;
    }
    var config = JSON.parse(configElement.textContent);
    window.Sentry.init({
      dsn: config.dsn,
      environment: config.environment,
      release: config.release,
    });
    if (config.git_sha) {
      window.Sentry.setTag('git_sha', config.git_sha);
    }
  } catch (error) {
    // Never let Sentry bootstrap break the page -- a reporting failure must not
    // take down the web UI itself.
    console.error('Failed to initialize frontend Sentry reporting:', error);
  }
})();
