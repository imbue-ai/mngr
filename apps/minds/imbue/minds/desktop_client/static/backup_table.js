// Shared backup-table row builder (settings + full-history). window.mindsBackupTable.
(function () {
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

  // Export restores the snapshot on this machine and streams a zip (works offline).
  function downloadSnapshot(agentId, link, snapshotId) {
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
        // A snapshot pruned between load and click fails the export here; show
        // it briefly then restore the affordance (a later refresh drops the
        // stale row).
        link.textContent = 'Download failed';
        setTimeout(function () { restore(original); }, 3000);
      });
  }

  // Relative time (+ Latest badge) on the left; Download on the right.
  // ``actions`` is a flex slot so a second action (e.g. Restore) can join later.
  function buildSnapshotRow(agentId, snapshot, isLatest, isFirst) {
    var row = document.createElement('div');
    row.className = 'flex items-center gap-4 px-4 py-3'
      + (isFirst ? '' : ' border-t border-default')
      + (isLatest ? ' bg-fill-hover' : '');

    var timeCell = document.createElement('div');
    timeCell.className = 'flex-1 flex items-center gap-2 min-w-0';

    var timeEl = document.createElement('span');
    timeEl.className = 'type-body text-primary';
    timeEl.textContent = relativeAgo(snapshot.time);
    timeEl.title = new Date(snapshot.time).toLocaleString();
    timeCell.appendChild(timeEl);

    if (isLatest) {
      var latestBadge = document.createElement('span');
      latestBadge.className = 'inline-flex items-center px-2 py-0.5 rounded-md type-label bg-success/15 text-success';
      latestBadge.textContent = 'Latest';
      timeCell.appendChild(latestBadge);
    }
    row.appendChild(timeCell);

    var actions = document.createElement('div');
    actions.className = 'flex items-center gap-4 shrink-0';

    var download = document.createElement('a');
    download.href = '#';
    download.className = 'type-body text-accent cursor-pointer';
    download.textContent = 'Download';
    download.addEventListener('click', function (ev) {
      ev.preventDefault();
      downloadSnapshot(agentId, download, snapshot.snapshot_id);
    });
    actions.appendChild(download);

    row.appendChild(actions);
    return row;
  }

  window.mindsBackupTable = {
    buildSnapshotRow: buildSnapshotRow,
  };
})();
