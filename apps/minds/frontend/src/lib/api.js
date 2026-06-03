// Typed fetch wrappers for the JSON endpoints FastAPI exposes. As the
// migration progresses, each form-post handler gets a function here
// that returns ``{ ok, errors, data }``. For Phase 1/2 the surface is
// intentionally minimal -- the trivial pages (welcome, login,
// login_redirect, auth_error) don't need any API calls.

async function postJson(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    credentials: 'same-origin',
  });
  const text = await response.text();
  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // Non-JSON response (rare; only happens on framework-level errors)
      // surfaces as a generic failure that callers can show inline.
      return { ok: false, errors: { _: `Invalid JSON response (status ${response.status})` } };
    }
  }
  if (!response.ok) {
    if (parsed && typeof parsed === 'object' && parsed.errors) {
      return { ok: false, errors: parsed.errors, data: parsed.data };
    }
    return { ok: false, errors: { _: parsed?.error || `HTTP ${response.status}` } };
  }
  // If the response speaks the `{ ok, errors, data }` envelope (the
  // convention every migrated form-post handler uses), unwrap it so
  // callers get the same shape on success and failure. Endpoints that
  // haven't been migrated yet just return their raw JSON and we pass
  // it through as `data`.
  if (parsed && typeof parsed === 'object' && 'ok' in parsed && 'errors' in parsed) {
    return { ok: !!parsed.ok, errors: parsed.errors || {}, data: parsed.data };
  }
  return { ok: true, errors: {}, data: parsed };
}

async function getJson(path) {
  const response = await fetch(path, {
    method: 'GET',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    return { ok: false, errors: { _: `HTTP ${response.status}` } };
  }
  const data = await response.json();
  return { ok: true, errors: {}, data };
}

export const api = { postJson, getJson };
