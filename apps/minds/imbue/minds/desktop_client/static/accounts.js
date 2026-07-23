// Accounts page: loads each account's plan/usage fragment asynchronously so
// the page itself paints instantly. The fragment is server-rendered
// (Jinja-autoescaped) HTML from GET /accounts/<user_id>/plan-view -- the slow
// part is the connector's live usage computation, which now happens here
// instead of blocking the page render. While a backup trim is running the
// fragment carries data-trim-running="1" and we keep re-fetching it so
// progress stays visible (replacing the old full-page reload loop).
(function () {
  var TRIM_POLL_MS = 4000;

  function showUnavailable(section) {
    section.textContent = '';
    var msg = document.createElement('div');
    msg.className = 'type-helper text-tertiary';
    msg.textContent = 'Plan and usage are unavailable right now (could not reach Imbue Cloud).';
    section.appendChild(msg);
  }

  function load(section) {
    var userId = section.dataset.userId;
    fetch('/accounts/' + encodeURIComponent(userId) + '/plan-view')
      .then(function (resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.text();
      })
      .then(function (html) {
        section.innerHTML = html;
        if (section.querySelector('[data-trim-running="1"]')) {
          setTimeout(function () { load(section); }, TRIM_POLL_MS);
        }
      })
      .catch(function () { showUnavailable(section); });
  }

  document.querySelectorAll('[data-plan-section]').forEach(load);
})();
