// Shared client-side normalizer for Minds API error responses.
//
// The API has two error body shapes, and any consumer that displays a server
// error needs to handle both:
//   - The uniform structural-validation contract (spectree): HTTP 422 with
//     {"errors": [{"field": "<dotted loc>", "message": "..."}, ...]}.
//   - Handler-emitted semantic errors: {"error": "...", "field"?: "...",
//     "redirect_url"?: "..."} (e.g. the create form's inline field errors and
//     the imbue_cloud sign-up backstop), or a bare {"detail": "..."}.
//
// normalizeApiError collapses either shape into a single {message, field,
// redirect_url} so callers have one thing to render. ``field`` (when present)
// matches the offending form input's id, so the create form can ring it.
(function () {
  function normalizeApiError(data) {
    if (!data || typeof data !== 'object') {
      return { message: 'Something went wrong. Please try again.', field: null, redirect_url: null };
    }
    // Structural-validation 422 contract: surface every failure's message and
    // the first offending field (which a form can use to highlight an input).
    if (Array.isArray(data.errors) && data.errors.length > 0) {
      var messages = data.errors
        .map(function (entry) { return entry && entry.message; })
        .filter(Boolean);
      var firstWithField = data.errors.find(function (entry) { return entry && entry.field; });
      return {
        message: messages.join('; ') || 'Some fields are invalid.',
        field: firstWithField ? firstWithField.field : null,
        redirect_url: null,
      };
    }
    // Handler-emitted semantic error shapes.
    var message =
      (typeof data.error === 'string' && data.error) ||
      (typeof data.detail === 'string' && data.detail) ||
      null;
    return {
      message: message || 'Something went wrong. Please try again.',
      field: typeof data.field === 'string' ? data.field : null,
      redirect_url: typeof data.redirect_url === 'string' ? data.redirect_url : null,
    };
  }

  window.normalizeApiError = normalizeApiError;
})();
