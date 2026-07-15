// Shared row builder for the backup tables: the "Recent backups" table on
// the workspace settings page (workspace_backups.js) and the paginated
// full-history page (workspace_backup_history.js). Living in one place
// guarantees the two tables cannot drift apart visually.
//
// Exposed as window.mindsBackupTable (same convention as mindsAccent /
// mindsBackupHealth); include this script before the page script that uses it.
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

  // Download one snapshot as a zip. Ported from the Landing "download" flow
  // (window.backupExport): the export route restores the snapshot on this
  // machine and streams it back, so this works even for an offline workspace.
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
        // A snapshot pruned between load and click 404s here; show it briefly
        // then restore the affordance (a later refresh drops the stale row).
        link.textContent = 'Download failed';
        setTimeout(function () { restore(original); }, 3000);
      });
  }

  // One table row: relative time (exact local time on hover) plus a "Latest"
  // badge on the newest snapshot on the left, Download / Restore actions on
  // the right. Rows after the first carry a top divider; the latest row is
  // tinted. (Snapshot size is deliberately not shown: the workspace's restic
  // may predate the per-snapshot size summary, so there is no reliable size
  // to display.)
  //
  // restoreConfig controls the Restore action:
  //   { onRestore: fn }            -- live button; fn(snapshot) runs on click
  //   { disabledReason: string }   -- disabled, with the reason as tooltip
  //   null/undefined               -- disabled, pointing at the settings page
  //     (the full-history page does not host the restore flow)
  function buildSnapshotRow(agentId, snapshot, isLatest, isFirst, restoreConfig) {
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

    var restore = document.createElement('button');
    restore.type = 'button';
    restore.textContent = 'Restore';
    restore.className = 'backup-restore-btn bg-transparent border-0 p-0 type-body';
    if (restoreConfig && restoreConfig.onRestore) {
      restore.classList.add('text-accent', 'cursor-pointer', 'disabled:opacity-40', 'disabled:cursor-not-allowed');
      restore.addEventListener('click', function () {
        restoreConfig.onRestore(snapshot);
      });
    } else {
      restore.disabled = true;
      restore.classList.add('text-tertiary', 'cursor-not-allowed');
      restore.title = (restoreConfig && restoreConfig.disabledReason) || 'Restore from the settings page';
    }
    actions.appendChild(restore);

    row.appendChild(actions);
    return row;
  }

  window.mindsBackupTable = {
    buildSnapshotRow: buildSnapshotRow,
  };
})();
