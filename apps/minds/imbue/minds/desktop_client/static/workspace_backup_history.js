// Full backup-history page: one GET /api/v1/workspaces/<id>/backups per page,
// paging server-side with limit/offset so the payload stays a single page.
// Row markup is shared via backup_table.js.
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
  var total = 0;

  function setShown(el, isShown) {
    el.classList.toggle('hidden', !isShown);
  }

  function showStatus(message) {
    statusEl.textContent = message;
    setShown(statusEl, true);
    setShown(cardEl, false);
    setShown(paginationEl, false);
  }

  function renderPage(pageSnapshots) {
    rowsEl.textContent = '';

    if (total === 0) {
      showStatus('No backups yet. The first backup runs within the hour.');
      return;
    }

    setShown(statusEl, false);
    setShown(cardEl, true);
    pageSnapshots.forEach(function (snapshot, index) {
      rowsEl.appendChild(window.mindsBackupTable.buildSnapshotRow(agentId, snapshot, index === 0));
    });

    var first = offset + 1;
    var last = offset + pageSnapshots.length;
    rangeEl.textContent = 'Showing ' + first + '-' + last + ' of ' + total + ' backups';
    prevBtn.disabled = offset === 0;
    nextBtn.disabled = last >= total;
    setShown(paginationEl, total > PAGE_SIZE);
  }

  function loadPage() {
    showStatus('Loading backup history...');
    var url = '/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups'
      + '?limit=' + PAGE_SIZE + '&offset=' + offset;
    fetch(url)
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
        total = typeof entry.snapshots_total === 'number' ? entry.snapshots_total : 0;
        renderPage(entry.snapshots || []);
      })
      .catch(function () {
        showStatus('Could not load backup history.');
      });
  }

  prevBtn.addEventListener('click', function () {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadPage();
  });
  nextBtn.addEventListener('click', function () {
    offset += PAGE_SIZE;
    loadPage();
  });

  loadPage();
})();
