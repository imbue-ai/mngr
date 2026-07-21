// Normalizer for Minds API error responses (TypeScript port of
// static/api_errors.js, which remains only for the not-yet-migrated create
// form and dies with it).
//
// The API has two error body shapes, and any consumer that displays a server
// error needs to handle both:
//   - The uniform structural-validation contract (spectree): HTTP 422 with
//     {"errors": [{"field": "<dotted loc>", "message": "..."}, ...]}.
//   - Handler-emitted semantic errors: {"error": "...", "field"?: "...",
//     "redirect_url"?: "..."} or a bare {"detail": "..."}.
//
// normalizeApiError collapses either shape into a single {message, field,
// redirectUrl} so callers have one thing to render.

export interface NormalizedApiError {
  message: string;
  // The offending form input's id when the server named one (forms use it to
  // ring the input).
  field: string | null;
  redirectUrl: string | null;
}

const FALLBACK_MESSAGE = "Something went wrong. Please try again.";

interface ErrorEntry {
  field?: unknown;
  message?: unknown;
}

export function normalizeApiError(data: unknown): NormalizedApiError {
  if (typeof data !== "object" || data === null) {
    return { message: FALLBACK_MESSAGE, field: null, redirectUrl: null };
  }
  const body = data as { errors?: unknown; error?: unknown; detail?: unknown; field?: unknown; redirect_url?: unknown };
  // Structural-validation 422 contract: surface every failure's message and
  // the first offending field.
  if (Array.isArray(body.errors) && body.errors.length > 0) {
    const entries = body.errors as ErrorEntry[];
    const messages = entries
      .map((entry) => (typeof entry?.message === "string" ? entry.message : ""))
      .filter((message) => message !== "");
    const firstWithField = entries.find((entry) => typeof entry?.field === "string" && entry.field !== "");
    return {
      message: messages.length > 0 ? messages.join("; ") : "Some fields are invalid.",
      field: firstWithField !== undefined ? String(firstWithField.field) : null,
      redirectUrl: null,
    };
  }
  // Handler-emitted semantic error shapes.
  const message =
    (typeof body.error === "string" && body.error !== "" && body.error) ||
    (typeof body.detail === "string" && body.detail !== "" && body.detail) ||
    null;
  return {
    message: message ?? FALLBACK_MESSAGE,
    field: typeof body.field === "string" ? body.field : null,
    redirectUrl: typeof body.redirect_url === "string" ? body.redirect_url : null,
  };
}
