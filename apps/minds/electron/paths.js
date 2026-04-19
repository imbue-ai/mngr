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
