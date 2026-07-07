// Shared backup-health cache for the workspace-list surfaces (sidebar +
// chrome). Fetches /api/v1/workspaces/backup-health once on load (and then on
// a slow refresh cadence -- each fetch runs the full verification batch
// against online workspaces), keeps the latest per-workspace verdict, and
// notifies subscribers so rows can add/remove the backup warning badge.
//
// The badge appears only when a problem is detected (check_state PROBLEMS);
// OFFLINE / DISABLED / UNKNOWN / OK all render nothing.
(function () {
  var PROBLEM_LABELS = {
    NOT_CONFIGURED: 'backups are not configured',
    CODE_OUTDATED: 'the backup service is outdated',
    ENV_MISSING: 'backup credentials are missing',
    ENV_MISMATCH: 'backup credentials do not match',
    SERVICE_NOT_RUNNING: 'the backup service is not running',
    UNVERIFIABLE: 'the backup service could not be verified',
  };
  var REFRESH_INTERVAL_MS = 15 * 60 * 1000;

  var warningByAgentId = {};
  var listeners = [];

  function warningText(entry) {
    if (entry.check_state !== 'PROBLEMS') return null;
    var parts = (entry.problems || []).map(function (problem) {
      return PROBLEM_LABELS[problem] || problem;
    });
    if (parts.length === 0) return 'Backup problem detected.';
    var text = parts.join('; ');
    return 'Backup warning: ' + text.charAt(0).toUpperCase() + text.slice(1) + '.';
  }

  function ingest(data) {
    warningByAgentId = {};
    (data.workspaces || []).forEach(function (entry) {
      var text = warningText(entry);
      if (text) warningByAgentId[entry.agent_id] = text;
    });
    listeners.forEach(function (listener) {
      try { listener(); } catch (e) { /* one bad listener must not break the rest */ }
    });
  }

  function refresh() {
    fetch('/api/v1/workspaces/backup-health')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) { if (data) ingest(data); })
      .catch(function () {});
  }

  window.mindsBackupHealth = {
    // Returns the warning tooltip for a workspace, or null when no badge is due.
    get: function (agentId) { return warningByAgentId[agentId] || null; },
    onUpdate: function (listener) { listeners.push(listener); },
    ingest: ingest,
    refresh: refresh,
  };

  refresh();
  setInterval(refresh, REFRESH_INTERVAL_MS);
})();
