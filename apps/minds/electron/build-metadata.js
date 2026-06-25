const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const paths = require('./paths');

/**
 * Resolve the release id + git SHA handed to Sentry (the release + git_sha tag)
 * by both the Electron main process and the Python backend it spawns.
 *
 * - releaseId always comes from package.json (the desktop app version).
 * - gitSha: dev runs resolve it live from the monorepo's git checkout (this is
 *   `just minds-start` -> `pnpm start` -> `electron .`); packaged builds read
 *   the SHA baked into build-info.json by build.js (build-info.json is only
 *   written at build time, so reading it in dev would surface a stale value).
 * Both fall back to "unknown" when unavailable (e.g. a tarball with no .git, or
 *   a packaged build whose build-info.json is missing).
 */
function getBuildMetadata() {
  const releaseId = require('../package.json').version || 'unknown';
  let gitSha = 'unknown';
  if (paths.isDev()) {
    try {
      gitSha = execSync('git rev-parse HEAD', { cwd: paths.getMonorepoRoot() }).toString().trim() || 'unknown';
    } catch (err) {
      console.warn(`[build-metadata] Could not resolve git SHA from checkout: ${err.message}`);
    }
  } else {
    try {
      const info = JSON.parse(fs.readFileSync(path.join(__dirname, 'build-info.json'), 'utf8'));
      gitSha = info.gitSha || 'unknown';
    } catch (err) {
      console.warn(`[build-metadata] Could not read build-info.json: ${err.message}`);
    }
  }
  return { releaseId, gitSha };
}

module.exports = { getBuildMetadata };
