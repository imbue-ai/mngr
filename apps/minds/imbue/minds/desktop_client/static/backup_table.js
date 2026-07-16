// Shared backup-table row builder (settings + full-history). window.mindsBackupTable.
(function () {
  function relativeAgo(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return '';
    var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 60) return 'just now';
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

  // One table row: relative time on the left, Download / Restore actions on
  // the right. Rows after the first carry a top divider.
  //
  // restoreConfig controls the Restore action:
  //   { onRestore: fn }          -- live button; fn(snapshot) runs on click
  //   { disabledReason: string } -- disabled, with the reason as tooltip
  //   null/undefined             -- disabled (defensive default; every
  //                                 current caller passes one of the above)
  function buildSnapshotRow(agentId, snapshot, isFirst, restoreConfig) {
    var row = document.createElement('div');
    row.className = 'flex items-center gap-4 px-4 py-3'
      + (isFirst ? '' : ' border-t border-default');

    var timeCell = document.createElement('div');
    timeCell.className = 'flex-1 flex items-center gap-2 min-w-0';

    var timeEl = document.createElement('span');
    timeEl.className = 'type-body text-primary';
    timeEl.textContent = relativeAgo(snapshot.time);
    timeCell.appendChild(timeEl);

    // A completed restore appends a snapshot of the restored state, tagged
    // `restored` plus `restored-from:<source-iso>`. Labeling it "Restored from
    // <source time>" makes the timeline read like a version history: this row
    // is the restored version, and the source it came from is named inline.
    var tags = snapshot.tags || [];
    if (tags.indexOf('restored') !== -1) {
      var lineageTag = tags.filter(function (tag) { return tag.indexOf('restored-from:') === 0; })[0];
      var restoredLabel = document.createElement('span');
      restoredLabel.className = 'inline-flex items-center px-2 py-0.5 rounded-md type-label bg-fill-hover text-secondary';
      if (lineageTag) {
        var sourceIso = lineageTag.slice('restored-from:'.length);
        restoredLabel.textContent = 'Restored from ' + new Date(sourceIso).toLocaleString();
      } else {
        restoredLabel.textContent = 'Restored';
      }
      timeCell.appendChild(restoredLabel);
    }
    row.appendChild(timeCell);

    var actions = document.createElement('div');
    actions.className = 'flex items-center gap-4 shrink-0';

    var download = document.createElement('a');
    download.href = '#';
    download.className = 'type-body text-accent hover:underline cursor-pointer';
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
    // Lets the settings page find and relabel the row of an in-flight restore.
    restore.dataset.snapshotId = snapshot.snapshot_id;
    if (restoreConfig && restoreConfig.onRestore) {
      restore.classList.add('text-accent', 'cursor-pointer', 'disabled:opacity-40', 'disabled:cursor-not-allowed');
      restore.addEventListener('click', function () {
        restoreConfig.onRestore(snapshot);
      });
    } else {
      restore.disabled = true;
      restore.classList.add('text-tertiary', 'cursor-not-allowed');
      restore.title = (restoreConfig && restoreConfig.disabledReason) || 'This backup cannot be restored right now.';
    }
    actions.appendChild(restore);

    row.appendChild(actions);
    return row;
  }

  // The Restore action's config for one /backups entry, shared by both tables
  // so they cannot disagree about when Restore is offered. A restore execs
  // into the workspace, so it needs the workspace reachable; Download works
  // regardless, because restic runs on this machine against the repository.
  function restoreConfigFor(entry, onRestore) {
    if (entry.check_state === 'OFFLINE') {
      return { disabledReason: 'This workspace is offline; start it to restore a backup.' };
    }
    return { onRestore: onRestore };
  }

  // Wire the shared restore-confirmation dialog (markup shipped by every
  // page that offers Restore, via the RestoreDialog template component) and
  // return the function that opens it for one snapshot. What a confirmed
  // restore *does* is the caller's business, though both current callers run
  // the tracked operation in place through the shared operation strip.
  function setupRestoreDialog(onConfirm) {
    var dialog = document.getElementById('restore-dialog');
    var timeEl = document.getElementById('restore-dialog-time');
    var cancelBtn = document.getElementById('restore-cancel-btn');
    var confirmBtn = document.getElementById('restore-confirm-btn');
    // The snapshot the open dialog is about.
    var pendingSnapshot = null;
    function close() { dialog.classList.add('hidden'); }
    cancelBtn.addEventListener('click', close);
    dialog.addEventListener('click', function (e) {
      if (e.target === dialog) close();
    });
    confirmBtn.addEventListener('click', function () {
      close();
      if (pendingSnapshot) onConfirm(pendingSnapshot);
    });
    return function (snapshot) {
      pendingSnapshot = snapshot;
      timeEl.textContent = new Date(snapshot.time).toLocaleString();
      dialog.classList.remove('hidden');
    };
  }

  window.mindsBackupTable = {
    buildSnapshotRow: buildSnapshotRow,
    restoreConfigFor: restoreConfigFor,
    setupRestoreDialog: setupRestoreDialog,
  };
})();
