// Per-agent health polling + unified restart affordance for the landing
// page. On load, probes each row's workspace server through the desktop
// client's /api/agents/{id}/health endpoint (which has a short per-probe
// timeout so polling N wedged minds doesn't stall the page). Rows flip
// between healthy / stuck / restarting states as probes come back.
//
// Interaction model:
//   row click  (healthy, unknown) -> navigate to /goto/{id}/
//   row click  (stuck)            -> auto-restart; navigate once healthy
//   row click  (restarting)       -> no-op (already pending)
//   icon click (any state)        -> force-restart; stay on this page
//
// The click-on-dead path exists because the previous behavior ("click a
// dead mind, nothing happens") gave the user no way to recover without
// knowing about the icon button. Subsuming it into the row click means
// you can't click a dead mind and get stuck staring at a blank page.

(function () {
  'use strict';

  var POLL_INTERVAL_MS = 2000;

  var pollTimer = null;
  // Map of agent_id -> true for rows whose restart was initiated by a row
  // click (as opposed to the icon button), meaning we should auto-navigate
  // when the restart completes. Icon-initiated restarts are "force restart
  // this mind but I'm not trying to enter it."
  var navigateOnRecovery = Object.create(null);

  function eachRow(fn) {
    var rows = document.querySelectorAll('.landing-row[data-agent-id]');
    for (var i = 0; i < rows.length; i += 1) fn(rows[i]);
  }

  function setRowHealth(row, health) {
    row.setAttribute('data-health', health);
    var stuckLabel = row.querySelector('.landing-status-stuck');
    var restartingLabel = row.querySelector('.landing-status-restarting');
    if (stuckLabel) stuckLabel.classList.toggle('hidden', health !== 'STUCK');
    if (restartingLabel) restartingLabel.classList.toggle('hidden', health !== 'RESTARTING');
    var dim = health === 'STUCK' || health === 'RESTARTING';
    row.classList.toggle('opacity-70', dim);
    row.classList.toggle('cursor-wait', health === 'RESTARTING');
    row.classList.toggle('cursor-pointer', health !== 'RESTARTING');
  }

  function probe(agentId) {
    return fetch('/api/agents/' + encodeURIComponent(agentId) + '/health', {
      credentials: 'same-origin',
    })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .catch(function () { return null; });
  }

  function applyProbeResult(row, body) {
    if (!body || !body.status) return;
    var agentId = row.getAttribute('data-agent-id');
    var previous = row.getAttribute('data-health');
    setRowHealth(row, body.status);
    if (body.status === 'HEALTHY' && previous === 'RESTARTING' && navigateOnRecovery[agentId]) {
      delete navigateOnRecovery[agentId];
      window.location = '/goto/' + encodeURIComponent(agentId) + '/';
    }
  }

  function ensurePolling() {
    if (pollTimer !== null) return;
    pollTimer = setInterval(function () {
      var anyRestarting = false;
      eachRow(function (row) {
        if (row.getAttribute('data-health') !== 'RESTARTING') return;
        anyRestarting = true;
        var agentId = row.getAttribute('data-agent-id');
        probe(agentId).then(function (body) { applyProbeResult(row, body); });
      });
      if (!anyRestarting) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }, POLL_INTERVAL_MS);
  }

  function triggerRestart(row, options) {
    var agentId = row.getAttribute('data-agent-id');
    if (!agentId) return;
    if (options && options.navigateAfter) navigateOnRecovery[agentId] = true;
    setRowHealth(row, 'RESTARTING');
    fetch('/api/agents/' + encodeURIComponent(agentId) + '/restart-workspace-server', {
      method: 'POST',
      credentials: 'same-origin',
    })
      .then(function (resp) {
        if (!resp.ok) {
          // Restart request itself failed; flip back to stuck so the user
          // gets their click affordance back. The tracker will also have
          // been flipped to stuck on the server side.
          setRowHealth(row, 'STUCK');
          delete navigateOnRecovery[agentId];
        }
      })
      .catch(function () {
        setRowHealth(row, 'STUCK');
        delete navigateOnRecovery[agentId];
      });
    ensurePolling();
  }

  function handleRowClick(row, event) {
    if (event.target.closest('.landing-restart-btn')) return;
    if (event.target.closest('button[aria-label="Workspace settings"]')) return;
    var health = row.getAttribute('data-health');
    var agentId = row.getAttribute('data-agent-id');
    if (health === 'RESTARTING') return;
    if (health === 'STUCK') {
      triggerRestart(row, { navigateAfter: true });
      return;
    }
    window.location = '/goto/' + encodeURIComponent(agentId) + '/';
  }

  document.addEventListener('click', function (event) {
    var row = event.target.closest('.landing-row[data-agent-id]');
    if (!row) return;
    var restartBtn = event.target.closest('.landing-restart-btn');
    if (restartBtn) {
      event.stopPropagation();
      triggerRestart(row, { navigateAfter: false });
      return;
    }
    handleRowClick(row, event);
  });

  // Initial probe of every row in parallel. Each probe is already short
  // (3s per-probe server-side timeout) so this resolves quickly even when
  // several minds are wedged.
  eachRow(function (row) {
    var agentId = row.getAttribute('data-agent-id');
    probe(agentId).then(function (body) { applyProbeResult(row, body); });
  });
})();
