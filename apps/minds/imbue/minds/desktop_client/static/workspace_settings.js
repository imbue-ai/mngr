// Workspace settings page: handles disassociate + (optional) Telegram
// setup. Reads the agent id from the #workspace-settings container's
// data-agent-id attribute so the template does not have to interpolate
// anything into JS.
(function () {
  var root = document.getElementById('workspace-settings');
  if (!root) return;
  var agentId = root.getAttribute('data-agent-id');
  if (!agentId) return;

  var disassociateBtn = document.getElementById('disassociate-btn');
  if (disassociateBtn) {
    disassociateBtn.addEventListener('click', function () {
      var spinner = document.getElementById('disassociate-spinner');
      disassociateBtn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');
      var section = document.getElementById('account-section');
      if (section) {
        section.style.opacity = '0.5';
        section.style.pointerEvents = 'none';
      }
      fetch('/workspace/' + encodeURIComponent(agentId) + '/disassociate', { method: 'POST' })
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          alert('Failed: ' + err.message);
          disassociateBtn.disabled = false;
          if (spinner) spinner.classList.add('hidden');
          if (section) {
            section.style.opacity = '1';
            section.style.pointerEvents = 'auto';
          }
        });
    });
  }

  var deleteBtn = document.getElementById('delete-workspace-btn');
  if (deleteBtn) {
    deleteBtn.addEventListener('click', function () {
      // `mngr destroy --gc` is not reversible: VM, host record, agent state,
      // and mngr/<name> branch all go away. Confirm hard.
      if (!confirm(
        'Permanently delete this workspace?\n\n' +
        'This destroys the VM, removes the agent, and deletes the ' +
        'mngr/<name> branch. It cannot be undone.'
      )) {
        return;
      }
      var spinner = document.getElementById('delete-workspace-spinner');
      deleteBtn.disabled = true;
      if (spinner) spinner.classList.remove('hidden');
      fetch('/workspace/' + encodeURIComponent(agentId) + '/delete', { method: 'POST' })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().then(function (data) {
              throw new Error(data.error || ('HTTP ' + resp.status));
            });
          }
          return resp.json();
        })
        .then(function (data) {
          window.location.href = data.redirect_url || '/';
        })
        .catch(function (err) {
          alert('Failed to delete workspace: ' + err.message);
          deleteBtn.disabled = false;
          if (spinner) spinner.classList.add('hidden');
        });
    });
  }

  var tgBtn = document.getElementById('tg-btn');
  if (tgBtn) {
    tgBtn.addEventListener('click', async function () {
      tgBtn.disabled = true;
      tgBtn.textContent = 'Setting up...';
      try {
        var resp = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/telegram/setup', { method: 'POST' });
        if (!resp.ok) {
          var data = await resp.json();
          alert('Failed: ' + (data.error || resp.statusText));
          tgBtn.disabled = false;
          tgBtn.textContent = 'Setup Telegram';
          return;
        }
        var interval = setInterval(async function () {
          try {
            var r = await fetch('/api/agents/' + encodeURIComponent(agentId) + '/telegram/status');
            if (!r.ok) return;
            var d = await r.json();
            if (d.status === 'DONE') {
              clearInterval(interval);
              tgBtn.textContent = 'Telegram active';
              tgBtn.classList.add('text-emerald-700');
            } else if (d.status === 'FAILED') {
              clearInterval(interval);
              tgBtn.textContent = 'Setup failed';
              tgBtn.disabled = false;
            } else {
              tgBtn.textContent = d.status;
            }
          } catch (e) { /* transient polling error */ }
        }, 2000);
      } catch (e) {
        alert('Failed: ' + e.message);
        tgBtn.disabled = false;
        tgBtn.textContent = 'Setup Telegram';
      }
    });
  }
})();
