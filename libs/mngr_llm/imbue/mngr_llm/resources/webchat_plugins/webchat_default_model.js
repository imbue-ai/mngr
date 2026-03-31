// Default model plugin for llm-webchat.
//
// Fetches the server-configured default model from /api/default-model and
// seeds the browser's localStorage so the model selector picks it up when
// the user has not yet made a manual choice.

(function () {
  "use strict";

  var LOCAL_STORAGE_KEY = "llm-webchat-selected-model";

  // Only seed if the user has not already chosen a model.
  if (localStorage.getItem(LOCAL_STORAGE_KEY)) {
    return;
  }

  var basePath = "";
  var meta = document.querySelector('meta[name="llm-webchat-base-path"]');
  if (meta) {
    basePath = meta.getAttribute("content") || "";
  }

  fetch(basePath + "/api/default-model")
    .then(function (response) {
      if (!response.ok) {
        return;
      }
      return response.json();
    })
    .then(function (data) {
      if (data && data.model_id) {
        // Only seed if still empty (avoid race with user interaction).
        if (!localStorage.getItem(LOCAL_STORAGE_KEY)) {
          localStorage.setItem(LOCAL_STORAGE_KEY, data.model_id);
        }
      }
    })
    .catch(function () {
      // Silently ignore network errors.
    });
})();
