/**
 * Injected-message handler plugin for llm-webchat.
 *
 * Listens for "injected_message" events on the SSE stream and inserts the
 * response directly into the UI via $llm.insertResponse, avoiding a full
 * page reload.
 */
window.addEventListener("load", function () {
  "use strict";

  $llm.on("stream_event", function (payload) {
    if (payload && payload.event && payload.event.type === "injected_message") {
      var response = payload.event.response;
      if (response && response.conversation_id) {
        $llm.insertResponse(response.conversation_id, response);
      }
      return payload;
    }
    return payload;
  });
});
