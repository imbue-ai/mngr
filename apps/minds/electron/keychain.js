// macOS keychain reads, owned by the Electron main process.
//
// Each `security find-generic-password` call runs as the calling binary's
// identity. When mngr_claude's Python subprocess does the read, the macOS
// SecurityServer prompt is attributed to Python (not minds.app), parented
// to nothing in particular, and "Always Allow" stamps the Python binary's
// signature into the entry's ACL -- which doesn't survive a minds.app
// rebuild. Reading here instead means the prompt identifies minds.app,
// the ACL grant tracks minds.app's stable ToDesktop signing cert, and a
// single prompt covers every future read in this and subsequent launches.
//
// The plumbing on top of this file passes the resolved value to mngr via
// the subprocess env so the Python side never has to call `security`
// itself.

const { execFile } = require('child_process');

// macOS's own SecurityServer prompt has its own multi-minute timeout; this
// is just a defensive ceiling so a pathological case can't hang Electron
// startup forever. NOT a UX deadline.
const KEYCHAIN_READ_TIMEOUT_MS = 5 * 60 * 1000;

// The keychain label Claude Code stores its API key under. mngr_claude reads
// the same label as the source of truth on the CLI path.
const CLAUDE_CODE_KEYCHAIN_LABEL = 'Claude Code';

/**
 * Read a macOS keychain entry by label. Resolves to the trimmed value, or
 * null if the entry doesn't exist, the user denied access, or this isn't
 * macOS. Never rejects.
 *
 * The macOS keychain ACL is the cache: once minds.app's signature is in
 * the entry's ACL, this returns the current value silently in microseconds.
 * Re-read every call rather than caching in-process so credential rotation
 * (e.g. user signs in again, claude rewrites the entry) is picked up
 * without a minds.app restart.
 */
function readKeychainCredential(label, { execFileImpl = execFile, platform = process.platform } = {}) {
  if (platform !== 'darwin') {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    execFileImpl(
      '/usr/bin/security',
      ['find-generic-password', '-l', label, '-w'],
      { timeout: KEYCHAIN_READ_TIMEOUT_MS },
      (err, stdout) => {
        if (err) {
          return resolve(null);
        }
        const value = String(stdout || '').trim();
        resolve(value || null);
      },
    );
  });
}

/**
 * Convenience wrapper for the "Claude Code" entry, which holds the
 * Anthropic API key on machines that have one provisioned. The OAuth
 * credentials JSON lives under "Claude Code-credentials" and is handled
 * in a follow-up.
 */
function readClaudeCodeApiKey(options) {
  return readKeychainCredential(CLAUDE_CODE_KEYCHAIN_LABEL, options);
}

module.exports = {
  CLAUDE_CODE_KEYCHAIN_LABEL,
  KEYCHAIN_READ_TIMEOUT_MS,
  readKeychainCredential,
  readClaudeCodeApiKey,
};
