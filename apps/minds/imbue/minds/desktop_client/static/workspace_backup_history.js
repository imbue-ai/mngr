// Full backup-history page: fills the table one page at a time from
// GET /api/v1/workspaces/<id>/backups/snapshots?offset=&limit= (newest-first,
// paginated server-side so a long history is never loaded all at once) and
// drives the Newer/Older pagination controls.
//
// The row markup mirrors the "Recent backups" table on the workspace settings
// page (workspace_backups.js), including the per-snapshot Download flow and
// the inert Restore placeholder.
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

  var PAGE_SIZE = 20;
  var offset = 0;

  function setShown(el, isShown) {
    el.classList.toggle('hidden', !isShown);
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return '--';
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var value = n / 1024;
    var i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
    return (value < 10 ? value.toFixed(1) : Math.round(value)) + ' ' + units[i];
  }

  function relativeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return '';
    var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 45) return 'just now';
    var m = Math.floor(s / 60);
    if (m < 60) return m + (m === 1 ? ' min ago' : ' mins ago');
    var h = Math.floor(m / 60);
    if (h < 24) return h + (h === 1 ? ' hour ago' : ' hours ago');
    var d = Math.floor(h / 24);
    if (d < 30) return d + (d === 1 ? ' day ago' : ' days ago');
    var mo = Math.floor(d / 30);
    if (mo < 12) return mo + (mo === 1 ? ' month ago' : ' months ago');
    var y = Math.floor(mo / 12);
    return y + (y === 1 ? ' year ago' : ' years ago');
  }

  // Download one snapshot as a zip (same flow as the settings page): the
  // export route restores the snapshot on this machine and streams it back,
  // so this works even for an offline workspace.
  function downloadSnapshot(link, snapshotId) {
    if (link.getAttribute('data-exporting') === '1') return;
    link.setAttribute('data-exporting', '1');
    link.style.pointerEvents = 'none';
    var original = link.textContent;
    link.innerHTML = '<span class="spinner spinner-accent inline-block w-3 h-3 align-middle"></span> Downloading...';
    function restore(text) {
      link.textContent = text;
      link.style.pointerEvents = '';
      link.removeAttribute('data-exporting');
    }
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/' + encodeURIComponent(snapshotId) + '/export', {
      method: 'POST',
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('export failed: ' + resp.status);
        var disp = resp.headers.get('Content-Disposition') || '';
        var match = /filename="?([^"]+)"?/.exec(disp);
        var name = match ? match[1] : (agentId + '-backup.zip');
        return resp.blob().then(function (blob) { return { blob: blob, name: name }; });
      })
      .then(function (result) {
        var url = URL.createObjectURL(result.blob);
        var anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = result.name;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
        restore(original);
      })
      .catch(function (err) {
        console.error('Backup export failed:', err);
        // A snapshot pruned between load and click 404s here; show it briefly
        // then restore the affordance (a later refresh drops the stale row).
        link.textContent = 'Download failed';
        setTimeout(function () { restore(original); }, 3000);
      });
  }

  // One table row: absolute time + "Latest" badge on the repository's newest
  // snapshot, size, Download / Restore actions. Unlike the settings page's
  // compact table this page shows the full date, with the relative age as
  // secondary text.
  function buildSnapshotRow(snapshot, isLatest, isFirst) {
    var row = document.createElement('div');
    row.className = 'flex items-center gap-4 px-4 py-3'
      + (isFirst ? '' : ' border-t border-default')
      + (isLatest ? ' bg-fill-hover' : '');

    var timeCell = document.createElement('div');
    timeCell.className = 'flex-1 flex items-center gap-2 min-w-0';

    var timeEl = document.createElement('span');
    timeEl.className = 'type-body text-primary';
    timeEl.textContent = new Date(snapshot.time).toLocaleString();
    timeCell.appendChild(timeEl);

    var agoEl = document.createElement('span');
    agoEl.className = 'type-helper text-tertiary';
    agoEl.textContent = relativeAgo(snapshot.time);
    timeCell.appendChild(agoEl);

    if (isLatest) {
      // Same green pill as the settings page / landing page badges.
      var latestBadge = document.createElement('span');
      latestBadge.className = 'inline-flex items-center px-2 py-0.5 rounded-md type-label bg-success/15 text-success';
      latestBadge.textContent = 'Latest';
      timeCell.appendChild(latestBadge);
    }
    row.appendChild(timeCell);

    var sizeEl = document.createElement('span');
    sizeEl.className = 'type-body text-secondary shrink-0';
    sizeEl.textContent = formatBytes(snapshot.total_size_bytes);
    row.appendChild(sizeEl);

    var actions = document.createElement('div');
    actions.className = 'flex items-center gap-4 shrink-0';

    var download = document.createElement('a');
    download.href = '#';
    download.className = 'type-body text-accent cursor-pointer';
    download.textContent = 'Download';
    download.addEventListener('click', function (ev) {
      ev.preventDefault();
      downloadSnapshot(download, snapshot.snapshot_id);
    });
    actions.appendChild(download);

    // In-place restore is not built yet; the button is shown but inert so the
    // design is visible without implying a working action.
    var restore = document.createElement('button');
    restore.type = 'button';
    restore.disabled = true;
    restore.className = 'type-body text-tertiary cursor-not-allowed bg-transparent border-0 p-0';
    restore.title = 'In-place restore is coming soon';
    restore.textContent = 'Restore';
    actions.appendChild(restore);

    row.appendChild(actions);
    return row;
  }

  function showStatus(message) {
    statusEl.textContent = message;
    setShown(statusEl, true);
    setShown(cardEl, false);
    setShown(paginationEl, false);
  }

  function renderPage(entry) {
    rowsEl.textContent = '';

    if (!entry.is_configured) {
      showStatus('Backups are turned off for this workspace.');
      return;
    }
    if (entry.error) {
      showStatus("Couldn't load your backup history right now.");
      return;
    }
    if (entry.total === 0) {
      showStatus('No backups yet. The first backup runs within the hour.');
      return;
    }

    setShown(statusEl, false);
    setShown(cardEl, true);
    entry.snapshots.forEach(function (snapshot, index) {
      // "Latest" marks the repository's newest snapshot, which only appears
      // on the first page.
      var isLatest = entry.offset === 0 && index === 0;
      rowsEl.appendChild(buildSnapshotRow(snapshot, isLatest, index === 0));
    });

    var first = entry.offset + 1;
    var last = entry.offset + entry.snapshots.length;
    rangeEl.textContent = 'Showing ' + first + '-' + last + ' of ' + entry.total + ' backups';
    prevBtn.disabled = entry.offset === 0;
    nextBtn.disabled = last >= entry.total;
    setShown(paginationEl, entry.total > entry.snapshots.length || entry.offset > 0);
  }

  function loadPage() {
    showStatus('Loading backup history...');
    fetch('/api/v1/workspaces/' + encodeURIComponent(agentId) + '/backups/snapshots?offset=' + offset + '&limit=' + PAGE_SIZE)
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (entry) {
        if (!entry) {
          showStatus('Could not load backup history.');
          return;
        }
        // A snapshot pruned between page loads can leave the offset past the
        // end; snap back to the last valid page instead of showing nothing.
        if (entry.total > 0 && entry.snapshots.length === 0 && offset > 0) {
          offset = Math.max(0, Math.floor((entry.total - 1) / PAGE_SIZE) * PAGE_SIZE);
          loadPage();
          return;
        }
        renderPage(entry);
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
