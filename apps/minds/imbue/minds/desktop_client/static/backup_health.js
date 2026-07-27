// Shared backup-health cache for the workspace-list surfaces (sidebar +
// chrome + landing). On load (and then on a slow refresh cadence) it fetches
// the workspace list once and fans out one per-workspace
// /api/v1/workspaces/<id>/backup-check request -- cross-workspace parallelism
// lives here in the frontend; the backend route is strictly per-workspace.
// It keeps the latest per-workspace verdict and notifies subscribers so rows
// can add/remove the backup warning badge.
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
    BACKUPS_STALE: 'backups have not run recently',
  };
  var REFRESH_INTERVAL_MS = 15 * 60 * 1000;

  // agent id -> { text, problems } for workspaces with a PROBLEMS verdict.
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

  function notifyListeners() {
    listeners.forEach(function (listener) {
      try { listener(); } catch (e) { /* one bad listener must not break the rest */ }
    });
  }

  // Ingest one workspace's /backup-check response (also called by the
  // settings page so its fresher result updates the badge immediately).
  function ingestEntry(entry) {
    if (!entry || !entry.agent_id) return;
    var text = warningText(entry);
    if (text) warningByAgentId[entry.agent_id] = { text: text, problems: (entry.problems || []).slice() };
    else delete warningByAgentId[entry.agent_id];
    notifyListeners();
  }

  function refresh() {
    fetch('/api/v1/workspaces')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        if (!data) return;
        var currentIds = {};
        (data.workspaces || []).forEach(function (workspace) {
          var agentId = workspace.agent_id || workspace.id;
          if (!agentId) return;
          currentIds[agentId] = true;
          // This surface reads only check_state/problems, so it hits the
          // check-only route (the snapshot listing lives on /backups).
          fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backup-check')
            .then(function (resp) { return resp.ok ? resp.json() : null; })
            .then(function (entry) { if (entry) ingestEntry(entry); })
            .catch(function () {});
        });
        // Drop warnings for workspaces that no longer exist (e.g. destroyed),
        // mirroring the per-refresh reset the old batch ingest performed.
        var removedAny = false;
        Object.keys(warningByAgentId).forEach(function (agentId) {
          if (!currentIds[agentId]) {
            delete warningByAgentId[agentId];
            removedAny = true;
          }
        });
        if (removedAny) notifyListeners();
      })
      .catch(function () {});
  }

  window.mindsBackupHealth = {
    // Returns the warning tooltip for a workspace, or null when no badge is due.
    get: function (agentId) {
      var entry = warningByAgentId[agentId];
      return entry ? entry.text : null;
    },
    // Returns the problem slugs behind the warning (empty when none), so
    // surfaces can react to specific causes (e.g. BACKUPS_STALE re-tones the
    // landing badge) without parsing the tooltip text.
    getProblems: function (agentId) {
      var entry = warningByAgentId[agentId];
      return entry ? entry.problems : [];
    },
    // Subscribes to verdict changes; returns an unsubscribe function. This
    // script outlives in-place page swaps (it belongs to the chrome shell's
    // persistent document), so swappable pages MUST unsubscribe on their
    // ``minds:page-teardown`` or their dead listeners accumulate forever.
    onUpdate: function (listener) {
      listeners.push(listener);
      return function () {
        var index = listeners.indexOf(listener);
        if (index !== -1) listeners.splice(index, 1);
      };
    },
    ingestEntry: ingestEntry,
    refresh: refresh,
  };

  refresh();
  setInterval(refresh, REFRESH_INTERVAL_MS);
})();
