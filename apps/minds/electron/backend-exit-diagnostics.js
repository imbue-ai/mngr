// What the startup error says when the backend dies before emitting its login
// URL. The error message becomes the full-window takeover's details and, when
// the user files a report from that screen, the Sentry event's extra.details --
// so it has to carry the datum that identifies the cause: the backend's own
// dying stderr (a uv `error:` line vs. a Python traceback vs. a click `Error:`),
// plus how the backend was launched. Pure so the rules are unit-testable under
// node.

const os = require('os');
const { isSecretStartupLogLine } = require('./startup-log');

/** How many of the backend's most recent stderr lines the error carries. */
const STDERR_TAIL_MAX_LINES = 20;
/** Ceiling on the tail's total size; the oldest lines are dropped first. */
const STDERR_TAIL_MAX_CHARS = 4096;

/**
 * Replace the user's home directory with `~`, so a traceback's file paths do
 * not carry the username into the takeover text or a report's event body.
 */
function scrubHomeDir(line, homeDir) {
  if (!homeDir || homeDir === '/') return line;
  // Only where the home directory is a whole path prefix: `/home/al` must not
  // turn `/home/alice` into `~ice`.
  const escaped = homeDir.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return line.replace(new RegExp(`${escaped}(?=$|[^\\w.-])`, 'g'), '~');
}

/**
 * A bounded ring of the backend's latest stderr lines.
 *
 * Blank lines are skipped (they would spend the line budget on nothing) and so
 * are lines carrying a credential, using the same rule as the loading
 * document's log. `homeDir` is injectable for tests.
 */
function createStderrTail({ homeDir = os.homedir() } = {}) {
  const lines = [];
  let totalChars = 0;
  return {
    push(line) {
      const trimmed = line.trimEnd();
      if (!trimmed.trim() || isSecretStartupLogLine(trimmed)) return;
      const scrubbed = scrubHomeDir(trimmed, homeDir).slice(-STDERR_TAIL_MAX_CHARS);
      lines.push(scrubbed);
      totalChars += scrubbed.length;
      while (lines.length > STDERR_TAIL_MAX_LINES || totalChars > STDERR_TAIL_MAX_CHARS) {
        totalChars -= lines.shift().length;
      }
    },
    lines() {
      return lines.slice();
    },
  };
}

/**
 * One line naming how the backend was launched: dev (the developer's own uv
 * against the monorepo) vs. packaged, whether --config-file was passed, and
 * whether MINDS_CLIENT_CONFIG_PATH was inherited (a boolean, never the path).
 */
function describeLaunch({ isDev, hasConfigFileArg, hasInheritedClientConfigEnv }) {
  const yesNo = (value) => (value ? 'yes' : 'no');
  return (
    `launch: ${isDev ? 'dev' : 'packaged'}, ` +
    `--config-file: ${yesNo(hasConfigFileArg)}, ` +
    `MINDS_CLIENT_CONFIG_PATH inherited: ${yesNo(hasInheritedClientConfigEnv)}`
  );
}

function formatStderrTail(stderrLines) {
  if (stderrLines.length === 0) return 'no stderr output';
  return ['last stderr lines:', ...stderrLines.map((line) => `  ${line}`)].join('\n');
}

/**
 * The startup error for a backend that exited before its login URL. The first
 * line keeps the established summary; the rest is the diagnosis.
 */
function formatEarlyExitDetails({ code, signal, elapsedMs, stdoutEventCount, launch, stderrLines }) {
  const status = signal ? `code ${code} (signal ${signal})` : `code ${code}`;
  return [
    `Backend exited with ${status} before emitting login URL`,
    `exited ${elapsedMs} ms after spawn; stdout JSONL events seen: ${stdoutEventCount}`,
    launch,
    formatStderrTail(stderrLines),
  ].join('\n');
}

/** The startup error for a backend that could not be spawned at all. */
function formatSpawnFailureDetails({ errorMessage, launch }) {
  return [`Failed to start backend: ${errorMessage}`, launch].join('\n');
}

module.exports = {
  STDERR_TAIL_MAX_LINES,
  STDERR_TAIL_MAX_CHARS,
  createStderrTail,
  describeLaunch,
  formatEarlyExitDetails,
  formatSpawnFailureDetails,
};
