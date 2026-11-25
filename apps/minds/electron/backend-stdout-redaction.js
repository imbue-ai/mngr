// What of the backend's stdout is masked before it is written to minds.log.
// Pure so the rules are unit-testable under node.

// Backend stdout JSONL event fields that carry secrets and must be masked
// before the raw line is written to minds.log (which is uploaded with bug
// reports). Keyed by event type; each value lists the fields to redact.
//
//   * mngr_forward_started: the freshly-minted mngr-forward preauth cookie --
//     a reusable, session-lifetime bearer token for the local forward. The
//     backend never logs it through any other path, so masking it here removes
//     it from the logs entirely.
//   * login_url: the backend's own preauth session cookie, the same kind of
//     token for the backend itself. Its URL needs no masking: a backend this
//     app starts mints no one-time code, so the URL carries none.
const SECRET_STDOUT_EVENT_FIELDS = {
  mngr_forward_started: ['preauth_cookie'],
  login_url: ['preauth_cookie'],
};

const REDACTED_PLACEHOLDER = '***';

/**
 * Return a copy of a backend stdout line safe to persist to minds.log.
 *
 * If the line is a JSON event carrying secrets (see SECRET_STDOUT_EVENT_FIELDS)
 * its secret fields are replaced with a placeholder; every other line is
 * returned unchanged. The parsed event used by the in-process handlers is
 * derived from the ORIGINAL line, so redaction never affects behavior -- only
 * what is written to disk.
 */
function redactStdoutLineForLog(line) {
  if (!line.trim()) return line;
  let event;
  try {
    event = JSON.parse(line);
  } catch {
    return line;
  }
  const secretFields = event && typeof event === 'object' ? SECRET_STDOUT_EVENT_FIELDS[event.event] : undefined;
  if (!secretFields) return line;
  const redacted = { ...event };
  for (const field of secretFields) {
    if (field in redacted) redacted[field] = REDACTED_PLACEHOLDER;
  }
  return JSON.stringify(redacted);
}

module.exports = { redactStdoutLineForLog };
