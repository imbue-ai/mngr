/**
 * Greeting conversation plugin for llm-webchat.
 *
 * Overrides the /new route so that clicking "New conversation" creates a
 * conversation pre-populated with a greeting message from the assistant,
 * then navigates to that conversation.  Falls back to the original /new
 * behavior if the greeting creation fails.
 */

window.addEventListener("load", function () {
  "use strict";

  // ── Base path ────────────────────────────────────────────────

  function getBasePath() {
    var meta = document.querySelector('meta[name="llm-webchat-base-path"]');
    return ((meta && meta.getAttribute("content")) || "").replace(/\/+$/, "");
  }

  var basePath = getBasePath();

  // ── State ────────────────────────────────────────────────────

  var isCreating = false;
  var errorMessage = null;

  // ── Helpers ──────────────────────────────────────────────────

  function navigateToConversation(conversationId) {
    history.pushState(
      null,
      "",
      basePath + "/conversations/" + encodeURIComponent(conversationId)
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  function navigateHome() {
    history.pushState(null, "", basePath + "/");
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  // ── Greeting creation ────────────────────────────────────────

  function createGreetingConversation(container) {
    if (isCreating) return;
    isCreating = true;
    errorMessage = null;
    renderLoading(container);

    fetch(basePath + "/api/greeting-conversation", { method: "POST" })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.error || "Server error " + response.status);
          });
        }
        return response.json();
      })
      .then(function (data) {
        isCreating = false;
        if (data.conversation_id) {
          navigateToConversation(data.conversation_id);
        } else {
          errorMessage = "No conversation ID returned";
          renderError(container);
        }
      })
      .catch(function (error) {
        isCreating = false;
        console.error("[greeting-plugin]", error);
        errorMessage = error.message || "Failed to create conversation";
        renderError(container);
      });
  }

  // ── Rendering ────────────────────────────────────────────────

  function renderLoading(container) {
    container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:16px;">' +
      '<div style="font-size:1.1em;color:var(--color-text-secondary,#888);">Creating conversation...</div>' +
      "</div>";
  }

  function renderError(container) {
    container.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:16px;">' +
      '<div style="font-size:1.1em;color:var(--color-text-secondary,#888);">Failed to create conversation</div>' +
      '<div style="font-size:0.9em;color:var(--color-text-faint,#aaa);">' +
      escapeHtml(errorMessage || "Unknown error") +
      "</div>" +
      '<button style="padding:8px 16px;cursor:pointer;border:1px solid var(--color-border,#444);' +
      'background:transparent;color:var(--color-text-primary,#ccc);border-radius:6px;"' +
      ' id="greeting-retry-btn">Try again</button>' +
      '<button style="padding:8px 16px;cursor:pointer;border:1px solid var(--color-border,#444);' +
      'background:transparent;color:var(--color-text-primary,#ccc);border-radius:6px;"' +
      ' id="greeting-back-btn">Go back</button>' +
      "</div>";

    var retryBtn = container.querySelector("#greeting-retry-btn");
    if (retryBtn) {
      retryBtn.addEventListener("click", function () {
        createGreetingConversation(container);
      });
    }

    var backBtn = container.querySelector("#greeting-back-btn");
    if (backBtn) {
      backBtn.addEventListener("click", function () {
        navigateHome();
      });
    }
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ── Route registration ───────────────────────────────────────

  $llm.registerRoute("/new", {
    render: function (container) {
      createGreetingConversation(container);
    },
    destroy: function () {
      isCreating = false;
      errorMessage = null;
    },
  });
});
