// The loading document's "Show details" log: which startup lines it may show,
// and how many it keeps. Pure so the rules are unit-testable under node; main.js
// filters through it before forwarding a line to the shell page.

/** Lines that carry a credential the page must not display (the app consumes them itself). */
const SECRET_LINE_PATTERNS = [/one_time_code=/i, /\btoken=/i, /\bpreauth-cookie\b/i, /\bbrowser-bridge-token\b/i];

/** The most lines the page keeps; older ones scroll off the top. */
const STARTUP_LOG_MAX_LINES = 400;

function isSecretStartupLogLine(line) {
  return SECRET_LINE_PATTERNS.some((pattern) => pattern.test(line));
}

/** Append `line` to `lines` in place, keeping only the newest STARTUP_LOG_MAX_LINES. */
function appendStartupLogLine(lines, line) {
  lines.push(line);
  if (lines.length > STARTUP_LOG_MAX_LINES) lines.splice(0, lines.length - STARTUP_LOG_MAX_LINES);
  return lines;
}

// Loaded by shell.html with a plain script tag (the renderer has no require),
// and required by main.js and the node unit tests.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { STARTUP_LOG_MAX_LINES, isSecretStartupLogLine, appendStartupLogLine };
}
if (typeof window !== 'undefined') {
  window.mindsStartupLog = { STARTUP_LOG_MAX_LINES, isSecretStartupLogLine, appendStartupLogLine };
}
