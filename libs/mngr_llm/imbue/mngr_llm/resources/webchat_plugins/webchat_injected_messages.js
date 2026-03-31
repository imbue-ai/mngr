/**
 * Injected-message handler plugin for llm-webchat.
 *
 * Listens for "injected_message" events on the SSE stream and inserts the
 * response directly into the UI via $llm.insertResponse, avoiding a full
 * page reload.
 *
 * The llm-webchat frontend only forwards `type`, `content`, and `model`
 * from raw SSE events into the stream_event hook payload, so the Python
 * side JSON-encodes the full response data into the `content` field.
 */
window.addEventListener("load", function () {
  "use strict";

  $llm.on("stream_event", function (payload) {
    if (payload && payload.event && payload.event.type === "injected_message") {
      var response = JSON.parse(payload.event.content);
      if (response && response.conversation_id) {
        $llm.insertResponse(response.conversation_id, response);
      }
      return payload;
    }
    return payload;
  });
});
