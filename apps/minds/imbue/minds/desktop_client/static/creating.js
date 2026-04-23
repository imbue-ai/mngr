// Agent creation progress page: streams the creation log over SSE and
// redirects to the agent when creation finishes. Reads the agent id from
// the #logs element's data-agent-id attribute so the template doesn't have
// to inline any JS.
(function () {
  var logsEl = document.getElementById('logs');
  if (!logsEl) return;
  var agentId = logsEl.getAttribute('data-agent-id');
  var statusEl = document.getElementById('status');
  var statusTextEl = document.getElementById('status-text');
  var source = new EventSource('/api/create-agent/' + agentId + '/logs');

  var pendingLines = [];
  var flushScheduled = false;

  function flushLogs() {
    flushScheduled = false;
    if (pendingLines.length === 0) return;
    var fragment = document.createDocumentFragment();
    fragment.appendChild(document.createTextNode(pendingLines.join('\n') + '\n'));
    pendingLines = [];
    logsEl.appendChild(fragment);
    logsEl.scrollTop = logsEl.scrollHeight;
  }

  source.onmessage = function (event) {
    try {
      var data = JSON.parse(event.data);
      if (data._type === 'done') {
        source.close();
        flushLogs();
        if (data.status === 'DONE' && data.redirect_url) {
          statusTextEl.textContent = 'Done. Redirecting...';
          window.location.href = data.redirect_url;
        } else if (data.status === 'FAILED') {
          statusTextEl.textContent = 'Failed: ' + (data.error || 'unknown error');
          statusEl.classList.add('text-red-600');
        }
      } else if (data.log) {
        pendingLines.push(data.log);
        if (!flushScheduled) {
          flushScheduled = true;
          requestAnimationFrame(flushLogs);
        }
      }
    } catch (e) {
      // ignore parse errors (keepalive comments etc)
    }
  };

  source.onerror = function () {
    source.close();
  };
})();
