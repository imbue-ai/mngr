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

function getDataDir() {
  return path.join(os.homedir(), '.minds');
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

module.exports = {
  isDev,
  getResourcesDir,
  getUvPath,
  getUvBinDir,
  getGitPath,
  getGitBinDir,
  getDataDir,
  getUvCacheDir,
  getUvPythonDir,
  getLogDir,
  getVenvDir,
  getPyprojectDir,
};
