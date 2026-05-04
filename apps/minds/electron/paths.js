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
  return path.join(getDataDir(), 'latchkey');
}

function getMindsRootName() {
  const name = process.env.MINDS_ROOT_NAME || 'minds';
  if (!/^[a-z0-9_-]+$/.test(name)) {
    throw new Error(
      `MINDS_ROOT_NAME must match [a-z0-9_-]+; got ${JSON.stringify(name)}`
    );
  }
  return name;
}

function getDataDir() {
  return path.join(os.homedir(), '.' + getMindsRootName());
}

function getMngrHostDir() {
  return path.join(getDataDir(), 'mngr');
}

function getMngrPrefix() {
  return getMindsRootName() + '-';
}

function getUvCacheDir() {
  return path.join(getDataDir(), '.uv-cache');
}

function getUvPythonDir() {
  return path.join(getDataDir(), '.uv-python');
}

function getLogDir() {
  return path.join(getDataDir(), 'logs');
}

function getVenvDir() {
  return path.join(getDataDir(), '.venv');
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
  getLatchkeyPath,
  getLatchkeyDirectory,
  getMindsRootName,
  getDataDir,
  getMngrHostDir,
  getMngrPrefix,
  getUvCacheDir,
  getUvPythonDir,
  getLogDir,
  getVenvDir,
  getPyprojectDir,
  getMonorepoRoot,
};
