/**
 * Injected-message handler plugin for llm-webchat.
 *
 * Listens for "injected_message" events on the SSE stream and refreshes
 * the page so the user sees the newly injected message.
 */
window.addEventListener("load", function () {
  "use strict";

  $llm.on("stream_event", function (payload) {
    if (payload && payload.event && payload.event.type === "injected_message") {
      window.location.reload();
      return payload;
    }
    return payload;
  });
});
