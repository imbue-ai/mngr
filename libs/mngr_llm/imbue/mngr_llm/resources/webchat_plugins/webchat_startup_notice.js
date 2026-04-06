/**
 * Startup notice plugin for llm-webchat.
 *
 * Polls GET /api/agent-startup-status to detect when the thinking agent
 * has not yet started its Claude Code session (e.g. because it is blocked
 * on a startup dialog like the bypass-permissions confirmation).  Shows a
 * dismissible banner at the top of the page with instructions to resolve
 * the issue via `mngr connect`.
 */
window.addEventListener("load", function () {
  "use strict";

  var POLL_INTERVAL_MS = 3000;
  var BANNER_ID = "startup-notice-banner";

  // ── Base path ────────────────────────────────────────────────

  function getBasePath() {
    var meta = document.querySelector('meta[name="llm-webchat-base-path"]');
    return ((meta && meta.getAttribute("content")) || "").replace(/\/+$/, "");
  }

  var basePath = getBasePath();

  // ── State ────────────────────────────────────────────────────

  var pollTimer = null;
  var dismissed = false;

  // ── DOM helpers ──────────────────────────────────────────────

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function removeBanner() {
    var existing = document.getElementById(BANNER_ID);
    if (existing) {
      existing.remove();
    }
  }

  function showBanner(agentName) {
    if (dismissed) return;
    if (document.getElementById(BANNER_ID)) return;

    var banner = document.createElement("div");
    banner.id = BANNER_ID;
    banner.style.cssText = [
      "position: fixed",
      "top: 0",
      "left: 0",
      "right: 0",
      "z-index: 10000",
      "background: #fff3cd",
      "color: #664d03",
      "padding: 10px 16px",
      "font-size: 14px",
      "font-family: system-ui, -apple-system, sans-serif",
      "display: flex",
      "align-items: center",
      "justify-content: space-between",
      "border-bottom: 1px solid #ffecb5",
      "box-shadow: 0 1px 3px rgba(0,0,0,0.1)",
    ].join("; ");

    var command = agentName ? "mngr connect " + escapeHtml(agentName) : "mngr connect <agent-name>";

    banner.innerHTML =
      '<span>The thinking agent is waiting at a startup prompt. Run ' +
      '<code style="background: rgba(0,0,0,0.08); padding: 2px 6px; border-radius: 3px; font-size: 13px;">' +
      command +
      "</code>" +
      " to resolve it.</span>" +
      '<button style="background: none; border: none; cursor: pointer; font-size: 18px; color: #664d03; padding: 0 4px; margin-left: 12px; line-height: 1;" title="Dismiss">&times;</button>';

    banner.querySelector("button").addEventListener("click", function () {
      dismissed = true;
      removeBanner();
    });

    document.body.appendChild(banner);
  }

  // ── Polling ──────────────────────────────────────────────────

  function poll() {
    fetch(basePath + "/api/agent-startup-status")
      .then(function (response) {
        if (!response.ok) return null;
        return response.json();
      })
      .then(function (data) {
        if (!data) return;

        if (data.started) {
          removeBanner();
          stopPolling();
        } else {
          showBanner(data.agent_name);
        }
      })
      .catch(function () {
        // Silently ignore network errors during polling.
      });
  }

  function startPolling() {
    poll();
    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // ── Initialization ───────────────────────────────────────────

  $llm.on("ready", function () {
    startPolling();
  });
});
