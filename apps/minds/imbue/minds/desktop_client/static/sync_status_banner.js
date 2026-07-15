// Post-signin sync-status banner for the server-rendered app pages (landing,
// create, accounts). When an account signs in on a device with no locally
// synced records (fresh install, amnesia restore), the landing decision runs
// before the first record fetch completes, so a returning user can land on
// the create form while their remote workspaces are still in flight. This
// banner surfaces that window by polling /_chrome/sync-initial-status:
//   PENDING -> "Fetching synced workspaces..."
//   FAILED  -> "Couldn't fetch... retrying" (the scheduler loop retries)
//   DONE n>0 -> "Found n synced workspace(s) -- View" (dismissible)
//   DONE n=0 -> nothing (a genuinely record-less account; the common case)
// Polling runs only while an entry is unresolved. Dismissals are per app
// session (sessionStorage), and a DONE entry auto-dismisses on the landing
// page itself, where the tiles it points at are already visible.
(function () {
  var POLL_PENDING_MS = 2500;
  var POLL_FAILED_MS = 15000;
  var DISMISS_PREFIX = 'minds-sync-banner-dismissed:';
  var BANNER_ID = 'sync-initial-status-banner';

  function isDismissed(userId) {
    try { return sessionStorage.getItem(DISMISS_PREFIX + userId) === '1'; } catch (e) { return false; }
  }

  function dismiss(userId) {
    try { sessionStorage.setItem(DISMISS_PREFIX + userId, '1'); } catch (e) { /* banner just reappears */ }
  }

  function ensureBanner() {
    var banner = document.getElementById(BANNER_ID);
    if (banner) return banner;
    banner = document.createElement('div');
    banner.id = BANNER_ID;
    banner.setAttribute('role', 'status');
    banner.style.cssText =
      'position: sticky; top: 0; z-index: 50; display: none; align-items: center; gap: 8px;' +
      'padding: 8px 16px; background: var(--c-surface-secondary, #f0f0f0);' +
      'border-bottom: 1px solid var(--c-border-primary, rgba(128,128,128,0.25));';
    banner.className = 'type-body text-primary';
    document.body.insertBefore(banner, document.body.firstChild);
    return banner;
  }

  function hideBanner() {
    var banner = document.getElementById(BANNER_ID);
    if (banner) banner.style.display = 'none';
  }

  function messageFor(entry) {
    if (entry.state === 'PENDING') {
      return 'Fetching synced workspaces for ' + entry.email + '…';
    }
    if (entry.state === 'FAILED') {
      return 'Couldn’t fetch synced workspaces for ' + entry.email + ' — retrying…';
    }
    var count = entry.workspace_count || 0;
    return 'Found ' + count + ' synced workspace' + (count === 1 ? '' : 's') + ' for ' + entry.email + '.';
  }

  // Pick the single entry to show: any PENDING first (fetch under way), then
  // FAILED, then an undismissed DONE with workspaces. One line is enough --
  // multiple simultaneously-fresh signins are rare and resolve within seconds.
  function pickEntry(accounts) {
    var byPriority = { PENDING: [], FAILED: [], DONE: [] };
    accounts.forEach(function (entry) {
      if (!byPriority[entry.state]) return;
      if (entry.state === 'DONE' && ((entry.workspace_count || 0) === 0 || isDismissed(entry.user_id))) return;
      byPriority[entry.state].push(entry);
    });
    return byPriority.PENDING[0] || byPriority.FAILED[0] || byPriority.DONE[0] || null;
  }

  function render(accounts) {
    // On the landing page the tiles a DONE banner points at are already on
    // screen; auto-dismiss so it doesn't linger on later pages either.
    if (window.location.pathname === '/') {
      accounts.forEach(function (entry) {
        if (entry.state === 'DONE') dismiss(entry.user_id);
      });
    }
    var entry = pickEntry(accounts);
    if (!entry) {
      hideBanner();
      return;
    }
    var banner = ensureBanner();
    banner.textContent = '';
    var text = document.createElement('span');
    text.textContent = messageFor(entry);
    banner.appendChild(text);
    if (entry.state === 'DONE') {
      var view = document.createElement('a');
      view.href = '/';
      view.textContent = 'View';
      view.style.cssText = 'text-decoration: underline; font-weight: 500; color: inherit;';
      banner.appendChild(view);
      var close = document.createElement('button');
      close.type = 'button';
      close.setAttribute('aria-label', 'Dismiss');
      close.textContent = '×';
      close.style.cssText = 'margin-left: auto; background: none; border: none; cursor: pointer; color: inherit; font-size: 16px;';
      close.addEventListener('click', function () {
        dismiss(entry.user_id);
        hideBanner();
      });
      banner.appendChild(close);
    }
    banner.style.display = 'flex';
  }

  function poll() {
    fetch('/_chrome/sync-initial-status')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        // Unauthenticated (or error) responses end polling: signin reloads the page.
        if (!data || !Array.isArray(data.accounts)) return;
        render(data.accounts);
        var hasPending = data.accounts.some(function (entry) { return entry.state === 'PENDING'; });
        var hasFailed = data.accounts.some(function (entry) { return entry.state === 'FAILED'; });
        if (hasPending) setTimeout(poll, POLL_PENDING_MS);
        else if (hasFailed) setTimeout(poll, POLL_FAILED_MS);
      })
      .catch(function () { /* transient fetch failure: the next page load re-polls */ });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', poll);
  else poll();
})();
