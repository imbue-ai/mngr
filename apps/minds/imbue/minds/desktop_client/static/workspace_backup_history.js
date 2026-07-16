// Full backup-history page: one GET /api/v1/workspaces/<id>/backups, then
// client-side Newer/Older paging. Row markup is shared via backup_table.js.
(function () {
  var page = document.getElementById('backup-history-page');
  if (!page) return;
  var agentId = page.dataset.agentId;

  var statusEl = document.getElementById('history-status');
  var cardEl = document.getElementById('history-card');
  var rowsEl = document.getElementById('history-rows');
  var paginationEl = document.getElementById('history-pagination');
  var rangeEl = document.getElementById('history-range');
  var prevBtn = document.getElementById('history-prev-btn');
  var nextBtn = document.getElementById('history-next-btn');

  var PAGE_SIZE = 15;
  var offset = 0;
  var snapshots = [];

  function setShown(el, isShown) {
    el.classList.toggle('hidden', !isShown);
  }

  function showStatus(message) {
    statusEl.textContent = message;
    setShown(statusEl, true);
    setShown(cardEl, false);
    setShown(paginationEl, false);
  }

  function renderPage() {
    rowsEl.textContent = '';

    if (snapshots.length === 0) {
      showStatus('No backups yet. The first backup runs within the hour.');
      return;
    }

    var pageSnapshots = snapshots.slice(offset, offset + PAGE_SIZE);
    setShown(statusEl, false);
    setShown(cardEl, true);
    pageSnapshots.forEach(function (snapshot, index) {
      rowsEl.appendChild(window.mindsBackupTable.buildSnapshotRow(agentId, snapshot, index === 0));
    });

    var first = offset + 1;
    var last = offset + pageSnapshots.length;
    rangeEl.textContent = 'Showing ' + first + '-' + last + ' of ' + snapshots.length + ' backups';
    prevBtn.disabled = offset === 0;
    nextBtn.disabled = last >= snapshots.length;
    setShown(paginationEl, snapshots.length > PAGE_SIZE);
  }

  function loadHistory() {
    showStatus('Loading backup history...');
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (entry) {
        if (!entry) {
          showStatus('Could not load backup history.');
          return;
        }
        if (!entry.is_configured) {
          showStatus('Backups are turned off for this workspace.');
          return;
        }
        if (entry.snapshots_error) {
          showStatus("Couldn't load your backup history right now.");
          return;
        }
        snapshots = entry.snapshots || [];
        offset = 0;
        renderPage();
      })
      .catch(function () {
        showStatus('Could not load backup history.');
      });
  }

  prevBtn.addEventListener('click', function () {
    offset = Math.max(0, offset - PAGE_SIZE);
    renderPage();
  });
  nextBtn.addEventListener('click', function () {
    offset += PAGE_SIZE;
    renderPage();
  });

  loadHistory();
})();
