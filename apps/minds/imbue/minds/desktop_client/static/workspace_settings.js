// Workspace settings page: handles the color picker, disassociate, and
// (optional) Telegram setup. Reads the agent id from the
// #workspace-settings container's data-agent-id attribute so the
// template does not have to interpolate anything into JS.
(function () {
  var root = document.getElementById('workspace-settings');
  if (!root) return;
  var agentId = root.getAttribute('data-agent-id');
  if (!agentId) return;
  var isStale = root.getAttribute('data-is-stale') === 'true';

  // -- Color picker -------------------------------------------------------
  //
  // 12 unlabeled palette swatches + an always-visible hex input. The hex
  // input is the source of truth: selecting a swatch fills the input,
  // typing a valid hex sets the matching swatch (if any) to
  // aria-checked="true". Save is implicit -- a valid hex saves on blur,
  // a swatch click saves immediately; no Save button. SSE drives the
  // re-paint of the chrome / sidebar after each save.
  var hexInput = document.getElementById('color-hex-input');
  var swatchContainer = document.getElementById('color-swatches');
  var errorEl = document.getElementById('color-error');

  if (hexInput && swatchContainer && errorEl && !isStale) {
    var swatches = swatchContainer.querySelectorAll('.color-swatch');
    var lastSavedHex = (hexInput.value || '').toLowerCase();
    var hexPattern = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

    function normalizeHex(value) {
      var match = hexPattern.exec(String(value).trim());
      if (!match) return null;
      var body = match[1].toLowerCase();
      if (body.length === 3) {
        body = body.split('').map(function (ch) { return ch + ch; }).join('');
      }
      return '#' + body;
    }

    function showError(message) {
      errorEl.textContent = message;
      errorEl.classList.remove('hidden');
    }

    function clearError() {
      errorEl.textContent = '';
      errorEl.classList.add('hidden');
    }

    function syncSwatchSelection(normalized) {
      for (var i = 0; i < swatches.length; i++) {
        var sw = swatches[i];
        var checked = sw.getAttribute('data-color') === normalized;
        sw.setAttribute('aria-checked', checked ? 'true' : 'false');
      }
    }

    function setControlsDisabled(disabled) {
      hexInput.disabled = disabled;
      for (var i = 0; i < swatches.length; i++) {
        swatches[i].disabled = disabled;
      }
    }

    function saveColor(normalized) {
      // Idempotency: skip the POST when the user types the same value
      // that's already saved (e.g. blur after no edit).
      if (normalized === lastSavedHex) return;
      setControlsDisabled(true);
      fetch('/api/workspaces/' + encodeURIComponent(agentId) + '/color', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hex: normalized }),
      })
        .then(function (resp) {
          return resp.json().then(function (body) { return { ok: resp.ok, status: resp.status, body: body }; });
        })
        .then(function (result) {
          setControlsDisabled(false);
          if (result.ok) {
            lastSavedHex = normalized;
            hexInput.value = normalized;
            syncSwatchSelection(normalized);
            clearError();
            return;
          }
          var err = (result.body && result.body.error) || 'unknown';
          if (err === 'invalid_hex') {
            showError('That hex value is not valid. Use #rrggbb or #rgb.');
          } else if (err === 'not_primary') {
            showError("This agent isn't a primary workspace; color can't be set.");
          } else if (err === 'stale_provider') {
            showError('This workspace is currently unreachable; try again later.');
            setControlsDisabled(true);
          } else if (err === 'host_unreachable') {
            showError('Could not reach the workspace host. Try again in a moment.');
          } else {
            showError('Save failed (HTTP ' + result.status + ').');
          }
          // Revert the input to the last saved value so the picker
          // stays consistent with the persisted state.
          hexInput.value = lastSavedHex;
          syncSwatchSelection(lastSavedHex);
        })
        .catch(function (err) {
          setControlsDisabled(false);
          showError('Network error saving color: ' + err.message);
          hexInput.value = lastSavedHex;
          syncSwatchSelection(lastSavedHex);
        });
    }

    for (var i = 0; i < swatches.length; i++) {
      (function (sw) {
        sw.addEventListener('click', function () {
          var hex = sw.getAttribute('data-color');
          var normalized = normalizeHex(hex);
          if (!normalized) return;
          clearError();
          hexInput.value = normalized;
          syncSwatchSelection(normalized);
          saveColor(normalized);
        });
      })(swatches[i]);
    }

    hexInput.addEventListener('input', function () {
      var normalized = normalizeHex(hexInput.value);
      if (normalized === null) {
        // Mark invalid but defer the error message to blur so users
        // mid-typing don't get yelled at on every keystroke.
        clearError();
        return;
      }
      clearError();
      syncSwatchSelection(normalized);
    });

    hexInput.addEventListener('blur', function () {
      var normalized = normalizeHex(hexInput.value);
      if (normalized === null) {
        showError('That hex value is not valid. Use #rrggbb or #rgb.');
        hexInput.value = lastSavedHex;
        syncSwatchSelection(lastSavedHex);
        return;
      }
      hexInput.value = normalized;
      syncSwatchSelection(normalized);
      saveColor(normalized);
    });
  }
  // -- End color picker ---------------------------------------------------

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
