const fs = require('fs');
const path = require('path');
const os = require('os');
const { app } = require('electron');

/**
 * Resolve paths to bundled resources, accounting for asar packaging,
 * platform differences, and development mode.
 */

function isDev() {
  return !app.isPackaged;
}

function getResourcesDir() {
  if (isDev()) {
    return path.join(__dirname, '..', 'resources');
  }
  return process.resourcesPath;
}

function getUvPath() {
  return path.join(getResourcesDir(), 'uv', 'uv');
}

function getUvBinDir() {
  return path.dirname(getUvPath());
}

function getGitPath() {
  return path.join(getResourcesDir(), 'git', 'bin', 'git');
}

function getGitBinDir() {
  return path.dirname(getGitPath());
}

function getLimaPath() {
  return path.join(getResourcesDir(), 'lima', 'bin', 'limactl');
}

function getLimaBinDir() {
  return path.dirname(getLimaPath());
}

/**
 * Path to the bundled restic binary used by the desktop client to
 * provision and query per-workspace backup repositories.
 *
 * Both dev and packaged mode resolve to ``resources/restic/restic``:
 * build.js downloads restic per target platform into that location via
 * scripts/download-binaries.js. In dev, ``pnpm start`` runs the
 * ``prestart`` hook (``node scripts/download-binaries.js``) so the
 * binary is present before Electron boots, mirroring the bundled-app
 * UX -- a Minds end user (or dev) should never have to install restic
 * separately.
 */
function getResticPath() {
  return path.join(getResourcesDir(), 'restic', 'restic');
}

/**
 * Path to the Latchkey CLI shipped as an npm dependency of this app.
 *
 * Dev mode: pnpm installs the package into ``apps/minds/node_modules`` and
 * creates a ``.bin/latchkey`` wrapper (shebang ``#!/usr/bin/env node``). We
 * invoke that directly, so any developer who already has Node on PATH (a
 * prerequisite for running Electron itself) gets Latchkey for free.
 *
 * Packaged mode: build.js stages a fresh, flat ``npm install`` of latchkey
 * (including every platform-specific native prebuild) into
 * ``resources/latchkey/node_modules/`` and emits a small shim at
 * ``resources/latchkey/bin/latchkey``. The shim uses the packaged Electron
 * binary as Node (``ELECTRON_RUN_AS_NODE=1``) so we do not have to bundle a
 * second Node runtime. See ``scripts/build.js::bundleLatchkey`` for details.
 */
function getLatchkeyPath() {
  if (isDev()) {
    return path.join(__dirname, '..', 'node_modules', '.bin', 'latchkey');
  }
  return path.join(getResourcesDir(), 'latchkey', 'bin', 'latchkey');
}

/**
 * Directory where all minds-managed Latchkey gateways keep their shared
 * credential/config state (``LATCHKEY_DIRECTORY``). Sharing one directory
 * across gateways lets the user authenticate with each third-party service
 * once for all their agents, instead of once per agent.
 */
function getLatchkeyDirectory() {
  return path.join(getAppSupportDir(), 'latchkey');
}

/**
 * Path to the bundled config dir (apps/minds/imbue/minds/config/envs/_bundled/).
 *
 * Build-time bundleClientConfig() writes two files here when the build
 * env had MINDS_CLIENT_CONFIG_BUNDLE + MINDS_ROOT_NAME_BUNDLE set:
 * `client.toml` (the embedded per-env config) and `root_name` (the
 * MINDS_ROOT_NAME the runtime should export). When the build did NOT
 * set those (i.e. a dev-mode `pnpm start` / unflagged packaged build),
 * neither file exists and the runtime refuses to start without the
 * user activating an env in their shell first.
 *
 * Dev mode resolves to the source tree; packaged mode resolves under
 * the extra-resources pyproject dir that build.js syncs alongside the
 * pyproject.
 */
function getBundledConfigDir() {
  if (isDev()) {
    return path.join(__dirname, '..', 'imbue', 'minds', 'config', 'envs', '_bundled');
  }
  // In a packaged build, the entire `imbue/` tree is copied under the
  // pyproject staging dir by build.js, so `_bundled/` lives under the
  // resources path that backs `getPyprojectDir()`.
  return path.join(getPyprojectDir(), 'imbue', 'minds', 'config', 'envs', '_bundled');
}

/**
 * Return the absolute path to the bundled client.toml if the build
 * embedded one, otherwise null.
 */
function getBundledClientConfigPath() {
  const candidate = path.join(getBundledConfigDir(), 'client.toml');
  return fs.existsSync(candidate) ? candidate : null;
}

/**
 * Return the bundled MINDS_ROOT_NAME the runtime should export, or null
 * if the build did not embed one. Validated against the runtime regex
 * (`minds(-<env-name>)?`) so a corrupted bundle fails loudly here
 * instead of confusing the Python bootstrap.
 */
function getBundledMindsRootName() {
  const candidate = path.join(getBundledConfigDir(), 'root_name');
  if (!fs.existsSync(candidate)) {
    return null;
  }
  const raw = fs.readFileSync(candidate, 'utf8').trim();
  if (!/^minds(-[a-z0-9][a-z0-9_-]{0,38}[a-z0-9])?$/.test(raw)) {
    throw new Error(
      `bundled root_name file ${candidate} contains ${JSON.stringify(raw)}, which does not match ` +
        '`minds(-<env-name>)?`. Rebuild with a valid MINDS_ROOT_NAME_BUNDLE.'
    );
  }
  return raw;
}

/**
 * Resolve the MINDS_ROOT_NAME the runtime should run as.
 *
 * Precedence:
 *   1. The bundled root_name file (built into the app via
 *      MINDS_ROOT_NAME_BUNDLE) -- the production / staging / beta
 *      packaged-build case. Always wins so a user with a stale
 *      MINDS_ROOT_NAME export from a parent shell can't accidentally
 *      misdirect a packaged build.
 *   2. The process env MINDS_ROOT_NAME (the dev-mode `minds env activate`
 *      case). Validated against the runtime regex.
 *   3. Default to 'minds' (production) for the case where dev mode
 *      runs without activation (the Python backend will then refuse to
 *      start unless --config-file is passed -- by design).
 */
function getMindsRootName() {
  const bundled = getBundledMindsRootName();
  if (bundled) {
    return bundled;
  }
  const fromEnv = process.env.MINDS_ROOT_NAME;
  if (fromEnv) {
    if (!/^minds(-[a-z0-9][a-z0-9_-]{0,38}[a-z0-9])?$/.test(fromEnv)) {
      throw new Error(
        `MINDS_ROOT_NAME=${JSON.stringify(fromEnv)} does not match \`minds(-<env-name>)?\`. ` +
          'Activate a valid env via `eval "$(minds env activate <name>)"` or unset the var.'
      );
    }
    return fromEnv;
  }
  return 'minds';
}

/**
 * Tier subdirectory name for the active root name. Mirror of the Python
 * `minds_tier_for`: `minds` -> `production`, `minds-<env>` -> `<env>`.
 */
function getTier() {
  const rootName = getMindsRootName();
  if (rootName === 'minds') {
    return 'production';
  }
  return rootName.slice('minds-'.length);
}

function xdgBase(envVar, fallback) {
  const value = process.env[envVar];
  if (value && path.isAbsolute(value)) {
    return value;
  }
  return fallback;
}

/**
 * Resolve the four platform-canonical roots, mirroring the Python
 * `imbue.minds.bootstrap._minds_roots_for`:
 *   1. MINDS_DATA_HOME override -> $MINDS_DATA_HOME/<tier>/{app_support,cache,logs,config}
 *   2. darwin -> Apple Application Support / Caches / Logs
 *   3. otherwise (Linux) -> the XDG data / cache / state / config dirs
 */
function getMindsRoots() {
  const tier = getTier();
  const home = os.homedir();
  const override = process.env.MINDS_DATA_HOME;
  if (override) {
    const base = path.join(override, tier);
    return {
      appSupport: path.join(base, 'app_support'),
      cache: path.join(base, 'cache'),
      logs: path.join(base, 'logs'),
      config: path.join(base, 'config'),
    };
  }
  if (process.platform === 'darwin') {
    const appSupport = path.join(home, 'Library', 'Application Support', 'Minds', tier);
    return {
      appSupport,
      cache: path.join(home, 'Library', 'Caches', 'Minds', tier),
      logs: path.join(home, 'Library', 'Logs', 'Minds', tier),
      config: path.join(appSupport, 'config'),
    };
  }
  const dataHome = xdgBase('XDG_DATA_HOME', path.join(home, '.local', 'share'));
  const cacheHome = xdgBase('XDG_CACHE_HOME', path.join(home, '.cache'));
  const stateHome = xdgBase('XDG_STATE_HOME', path.join(home, '.local', 'state'));
  const configHome = xdgBase('XDG_CONFIG_HOME', path.join(home, '.config'));
  return {
    appSupport: path.join(dataHome, 'minds', tier),
    cache: path.join(cacheHome, 'minds', tier),
    logs: path.join(stateHome, 'minds', tier, 'logs'),
    config: path.join(configHome, 'minds', tier),
  };
}

function getAppSupportDir() {
  return getMindsRoots().appSupport;
}

function getCacheDir() {
  return getMindsRoots().cache;
}

function getConfigDir() {
  return getMindsRoots().config;
}

// Electron userData + window-state.json live under the app-support root.
function getDataDir() {
  return getAppSupportDir();
}

function getMngrHostDir() {
  return path.join(getAppSupportDir(), 'mngr');
}

function getMngrPrefix() {
  return getMindsRootName() + '-';
}

// The uv cache, managed python, and .venv are kept under the app-support
// root (not the cache root) so an OS cache purge can't delete the running
// app's interpreter out from under it.
function getUvCacheDir() {
  return path.join(getAppSupportDir(), '.uv-cache');
}

function getUvPythonDir() {
  return path.join(getAppSupportDir(), '.uv-python');
}

function getLogDir() {
  return getMindsRoots().logs;
}

function getVenvDir() {
  return path.join(getAppSupportDir(), '.venv');
}

function getPyprojectDir() {
  if (isDev()) {
    return path.join(__dirname, 'pyproject');
  }
  return path.join(getResourcesDir(), 'pyproject');
}

function getMonorepoRoot() {
  // apps/minds/electron/ -> apps/minds/ -> apps/ -> repo root
  return path.resolve(__dirname, '..', '..', '..');
}

module.exports = {
  isDev,
  getResourcesDir,
  getUvPath,
  getUvBinDir,
  getGitPath,
  getGitBinDir,
  getLimaPath,
  getLimaBinDir,
  getLatchkeyPath,
  getLatchkeyDirectory,
  getResticPath,
  getMindsRootName,
  getTier,
  getMindsRoots,
  getAppSupportDir,
  getCacheDir,
  getConfigDir,
  getDataDir,
  getMngrHostDir,
  getMngrPrefix,
  getUvCacheDir,
  getUvPythonDir,
  getLogDir,
  getVenvDir,
  getPyprojectDir,
  getMonorepoRoot,
  getBundledClientConfigPath,
  getBundledMindsRootName,
};
