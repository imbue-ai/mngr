'use strict';

// The environment the bundled dugite-native git needs, and the CA bundle it
// verifies TLS against.
//
// Free of any `electron` import so it is unit-testable under plain node.

const fs = require('fs');
const path = require('path');

/**
 * The certifi `cacert.pem` installed in the virtualenv at `venvDir`.
 *
 * The site-packages directory is named after the interpreter's major.minor,
 * read from the `version_info` line uv writes to `pyvenv.cfg`. Throws when
 * the venv, that line, or the pem is missing: git must never fall back to a
 * trust store nobody chose.
 */
function certifiBundlePathFor(venvDir) {
  const configPath = path.join(venvDir, 'pyvenv.cfg');
  if (!fs.existsSync(configPath)) {
    throw new Error(`no virtualenv at ${venvDir} (missing ${configPath}), so there is no certifi CA bundle for git`);
  }
  const versionMatch = fs.readFileSync(configPath, 'utf8').match(/^version_info\s*=\s*(\d+)\.(\d+)/m);
  if (!versionMatch) {
    throw new Error(`${configPath} has no version_info line, so the certifi CA bundle for git cannot be located`);
  }
  const pemPath = path.join(
    venvDir, 'lib', `python${versionMatch[1]}.${versionMatch[2]}`, 'site-packages', 'certifi', 'cacert.pem'
  );
  if (!fs.existsSync(pemPath)) {
    throw new Error(`the certifi CA bundle git verifies TLS against is missing: ${pemPath}`);
  }
  return pemPath;
}

/**
 * The payload binaries bake in an empty prefix, so prepending the bin dir to
 * PATH is not enough: `git --exec-path` would resolve to //libexec/git-core
 * and `git clone https://...` would fail with "'remote-https' is not a git
 * command". See specs/minds-managed-git/concise.md.
 *
 * `GIT_SSL_CAINFO` is the app's certifi bundle on every platform: the one
 * store we pin and refresh, so a CA removal reaches git with the next Python
 * environment update. Without it the Linux payload reads its own pem, which
 * moves only with dugite-native releases, and macOS reads /etc/ssl/cert.pem,
 * whose contents vary by OS version. On macOS the payload links Apple's
 * libcurl, whose LibreSSL falls back to the system trust (SecTrust: the
 * keychain plus Apple's own distrust list) when a chain fails the pem, so
 * there a chain is rejected only when both certifi and SecTrust reject it,
 * and keychain-only CAs (a corporate TLS proxy, say) keep working.
 */
function bundledGitEnv({ gitRoot, caBundlePath }) {
  return {
    GIT_EXEC_PATH: path.join(gitRoot, 'libexec', 'git-core'),
    GIT_TEMPLATE_DIR: path.join(gitRoot, 'share', 'git-core', 'templates'),
    GIT_CONFIG_SYSTEM: path.join(gitRoot, 'etc', 'gitconfig'),
    GIT_SSL_CAINFO: caBundlePath,
  };
}

module.exports = { certifiBundlePathFor, bundledGitEnv };
